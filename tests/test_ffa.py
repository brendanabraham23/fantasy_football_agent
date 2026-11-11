"""Offline tests: every network call is replaced with fixture data."""
from datetime import datetime, timezone

import pandas as pd
import pytest

from ffa import news, nflverse, pipeline, ranker, report, sleeper, weather
from conftest import PROJ, proj_line
from ffa.ranker import PlayerEval

NOW = datetime.now(timezone.utc)


def test_end_to_end(fake_world, tmp_path):
    res = pipeline.run("brendan", "Brendobendo")
    by = {e.name: e for e in res.roster}

    assert res.ctx.my_team_name == "Brendobendo"
    assert by["Golf WR"].adj == 0                       # Out
    assert by["Delta RB"].on_bye and by["Delta RB"].adj == 0
    assert by["Charlie RB"].team == "LA"                 # LAR normalized
    assert by["Bravo RB"].sentiment < 0 < by["Echo WR"].sentiment
    assert by["Echo WR"].mult_weather < 1                # 25mph wind hurts WR
    assert by["Bravo RB"].def_rank is None or by["Bravo RB"].opp == "KC"

    starters = {e.name for _, e in res.lineup if e}
    assert "Golf WR" not in starters and "Delta RB" not in starters

    recs = {r.player.name: r for r in res.waiver_recs}
    assert "Waiver Stud" in recs and recs["Waiver Stud"].weekly_gain > 0
    assert recs["Waiver Stud"].bid.endswith("of $60")
    assert "Meh WR" not in recs

    md, csv = report.save(res, tmp_path)
    text = md.read_text()
    assert "Recommended lineup" in text and "Waiver Stud" in text and "BENCH Delta RB" in text
    assert csv.exists()


def test_implied_totals():
    sched = pd.DataFrame([dict(game_id="g", week=1, game_type="REG", gameday="2026-09-13", home_team="A",
                               away_team="B", total_line=48.0, spread_line=6.0, location="Home")])
    m, avg = nflverse.matchups(sched, 1)
    assert m["A"]["implied"] == 27 and m["B"]["implied"] == 21 and avg == 24


def test_defense_rank_and_ratio():
    stats = pd.DataFrame([dict(season_type="REG", week=1, position="RB", opponent_team=t, fantasy_points=fp,
                               receptions=2) for t, fp in (("A", 10), ("B", 20), ("C", 30))])
    d = nflverse.defense_vs_position(stats, 2, rec_value=1.0).set_index("opponent_team")
    assert d.loc["A", "rank"] == 1 and d.loc["C", "rank"] == 3
    assert d.loc["A", "pts_allowed_pg"] == 12  # +2 receptions in full PPR
    assert d.loc["C", "ratio"] > 1 > d.loc["A", "ratio"]


def test_league_scoring():
    line = {"pass_yd": 300, "pass_td": 2, "rec": 5, "pts_ppr": 99}
    assert sleeper.fantasy_points(line, {"pass_yd": 0.04, "pass_td": 4, "rec": 0.5}, 0.5, "QB") == 22.5
    assert sleeper.fantasy_points({"pts_std": 9}, {"pts_allow_0": 10}, 0, "DEF") == 9


def test_sentiment_direction():
    s = news.NewsScorer({})
    bad = news.Article("Smith ruled out, placed on injured reserve", "", "x", "", NOW)
    good = news.Article("Smith cleared to play after full practice", "", "x", "", NOW)
    assert s.score_article(bad, "John Smith") < -0.5
    assert s.score_article(good, "John Smith") > 0.5
    assert s.score("John Smith", []).score == 0


def test_flex_lineup():
    mk = lambda pid, pos, adj: PlayerEval(pid, pid, pos, "X", adj=adj)
    evals = [mk("rb1", "RB", 20), mk("rb2", "RB", 15), mk("rb3", "RB", 14), mk("wr1", "WR", 12), mk("te1", "TE", 9)]
    lineup = dict((s + str(i), e.player_id) for i, (s, e) in enumerate(ranker.optimal_lineup(evals, ["RB", "RB", "WR", "TE", "FLEX", "BN"])))
    assert lineup["FLEX4"] == "rb3"


def test_weather_multiplier():
    windy = {"indoor": False, "wind_mph": 25, "precip_in_hr": 0, "snow_in_hr": 0, "temp_f": 50}
    assert weather.multiplier("QB", windy) < 1 < weather.multiplier("DEF", windy)
    assert weather.multiplier("WR", {"indoor": True}) == 1


WCFG = {"max_bid_pct": 0.35, "bid_horizon_weeks": 4, "bid_half_value": 30, "bid_demand_base": 0.5,
        "bid_competition_weight": 1.0, "bid_trend_weight": 0.15, "bid_cap_pct": 0.5}


def test_bids_scale_with_value_and_competition():
    from ffa.waivers import Market, suggest_bid
    cand = PlayerEval("x", "X", "RB", "KC", proj=12, trending_adds=0)
    lonely = Market(True, 100, 10, {2: 80, 3: 80}, {2: {"RB": 20}, 3: {"RB": 20}})   # nobody else would start him
    crowded = Market(True, 100, 10, {2: 80, 3: 80}, {2: {"RB": 5}, 3: {"RB": 5}})    # everyone would
    bids = [suggest_bid(WCFG, lonely, cand, w, r)[0] for w, r in ((1, 1), (4, 3), (8, 6))]
    assert bids[0] < bids[1] < bids[2]                                   # no more identical capped bids
    hot, detail = suggest_bid(WCFG, crowded, cand, 4, 3)
    assert hot > bids[1] and detail["rivals"] == 2 and detail["value"] == 16
    poor = Market(True, 100, 10, {2: 3, 3: 0}, {2: {"RB": 5}, 3: {"RB": 5}})       # one broke rival, one $3 rival
    capped, detail = suggest_bid(WCFG, poor, cand, 8, 6)
    assert capped == 4 and detail["rivals"] == 1 and detail["rival_max_budget"] == 3
    late = Market(True, 100, 1, {}, {})                                  # last regular-season week: no ROS value
    assert suggest_bid(WCFG, late, cand, 0, 6)[0] == 1


JEANTY = "Raiders Hoping Ashton Jeanty's Demotion Is 'Wake-Up Call' - Yahoo Sports"


def test_vader_fallback_scores_the_headline_once_and_knows_demotion():
    s = news.NewsScorer({})
    a = news.Article(JEANTY, JEANTY, "Yahoo Sports", "", NOW)            # Google News repeats the title as summary
    assert a.headline.endswith("'Wake-Up Call'") and a.scored_text == a.headline
    assert s.score_article(a, "Ashton Jeanty") < 0                      # was +0.68
    jr = news.Article("Michael Penix Jr. has worst outing of career", "", "x", "", NOW)
    assert s.score_article(jr, "Michael Penix Jr.") < 0                 # "Jr." no longer ends the sentence


def test_llm_scores_are_used_cached_and_fall_back(monkeypatch):
    from ffa import llm
    calls, store = [], {}
    monkeypatch.setattr(llm, "load_cache", lambda: dict(store))
    monkeypatch.setattr(llm, "save_cache", lambda c, keep: store.update(c))

    def fake(player, pos, team, titles, cfg):
        calls.append((player, pos, team, titles))
        return [-0.5] * len(titles)
    monkeypatch.setattr(llm, "score_headlines", fake)
    arts = [news.Article(JEANTY, JEANTY, "Yahoo Sports", "", NOW)]
    cfg = {"scorer": "llm", "llm": {"model": "sonnet"}}
    sent = news.NewsScorer(cfg).score("Ashton Jeanty", arts, "RB", "LV")
    assert sent.articles[0]["score"] == -0.5 and sent.articles[0]["scorer"] == "llm" and sent.score < 0
    assert calls == [("Ashton Jeanty", "RB", "LV", ["Raiders Hoping Ashton Jeanty's Demotion Is 'Wake-Up Call'"])]
    news.NewsScorer(cfg).score("Ashton Jeanty", arts, "RB", "LV")       # a later run reads the cache
    assert len(calls) == 1

    monkeypatch.setattr(llm, "score_headlines", lambda *a, **k: None)   # CLI missing or not logged in
    fresh = [news.Article("Ashton Jeanty ruled out for Sunday", "", "x", "", NOW)]
    sent = news.NewsScorer(cfg).score("Ashton Jeanty", fresh, "RB", "LV")
    assert sent.articles[0]["scorer"] == "vader" and sent.score < 0
    assert news.NewsScorer({}).score("Ashton Jeanty", arts).articles[0]["scorer"] == "vader"   # default stays offline


def test_usage_coefficients_and_expected():
    from ffa import usage
    players = {str(i): {"position": "WR" if i % 2 else "RB"} for i in range(40)}
    line = lambda i, wk: {"rec_tgt": (i + wk) % 9, "rush_att": (i * 3 + wk) % 14 if i % 2 == 0 else 0, "gp": 1}
    recent = {wk: {pid: line(int(pid), wk) for pid in players} for wk in (1, 2, 3)}
    for wk in recent.values():                      # WR: 1.8 a target; RB: 1.4 a target + 0.6 a carry
        for pid, l in wk.items():
            l["rush_yd"] = 10 * ((1.8 * l["rec_tgt"]) if players[pid]["position"] == "WR" else 1.4 * l["rec_tgt"] + 0.6 * l["rush_att"])
    coef = usage.coefficients(recent, players, {"rush_yd": 0.1}, 0, {"positions": ["RB", "WR", "TE"], "min_games": 20})
    assert coef["RB"] == pytest.approx((1.4, 0.6), abs=0.01) and coef["WR"][0] == pytest.approx(1.8, abs=0.01)
    assert "TE" not in coef                          # no TE games: the signal is off for that position
    assert usage.expected([{"rec_tgt": 10}, {"rec_tgt": 6, "rush_att": 2}], (1.5, 0.5)) == (12.5, 8.0, 1.0)
    assert usage.coefficients(recent, players, {"rush_yd": 0.1}, 0, {"positions": ["RB"], "min_games": 500}) == {}


def test_recent_form_follows_usage_not_points(fake_world, monkeypatch):
    """Two receivers with the same recent points: the one earning targets gets the higher base."""
    def stats(season, week):
        out = {k: {**proj_line(v), "rec_tgt": 5, "rush_att": 0} for k, v in PROJ.items()}
        out["5"] = {**proj_line(8), "rec_tgt": 11}       # Echo WR: heavy volume, modest points
        out["6"] = {**proj_line(8), "rec_tgt": 2}        # Foxtrot WR: same points on two targets
        return out
    monkeypatch.setattr(sleeper, "stats", stats)
    cfg = pipeline.load_config()
    cfg["usage"] = {"enabled": True, "weight": 1.0, "positions": ["WR"], "min_games": 3}
    res = pipeline.run("brendan", "Brendobendo", None, None, None, cfg, news=False, wx=False)
    by = {e.player_id: e for e in res.roster}
    hi, lo = by["5"], by["6"]
    assert hi.recent_avg == lo.recent_avg == 8 and hi.targets_avg == 11 and lo.targets_avg == 2
    assert hi.usage_pts > lo.usage_pts and hi.recent_form == hi.usage_pts
    assert hi.base - 0.7 * hi.proj > lo.base - 0.7 * lo.proj
    assert by["2"].usage_pts is None and by["2"].recent_form == by["2"].recent_avg   # RB not in positions: points only

    cfg["usage"]["enabled"] = False
    off = {e.player_id: e for e in pipeline.run("brendan", "Brendobendo", None, None, None, cfg, news=False, wx=False).roster}
    assert off["5"].usage_pts is None and off["5"].base == pytest.approx(0.7 * off["5"].proj + 0.3 * 8)
