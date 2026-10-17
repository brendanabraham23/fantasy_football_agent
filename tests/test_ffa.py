"""Offline tests: every network call is replaced with fixture data."""
from datetime import datetime, timezone

import pandas as pd

from ffa import news, nflverse, pipeline, ranker, report, sleeper, weather
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
