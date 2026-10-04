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
