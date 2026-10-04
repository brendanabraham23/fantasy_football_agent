"""Offline tests: every network call is replaced with fixture data."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ffa import news, nflverse, pipeline, ranker, report, sleeper, weather
from ffa.ranker import PlayerEval

NOW = datetime.now(timezone.utc)

PLAYERS = {
    "1": {"full_name": "Alpha QB", "position": "QB", "team": "KC", "active": True},
    "2": {"full_name": "Bravo RB", "position": "RB", "team": "BUF", "active": True},
    "3": {"full_name": "Charlie RB", "position": "RB", "team": "LAR", "active": True,
          "injury_status": "Questionable", "injury_body_part": "Ankle"},
    "4": {"full_name": "Delta RB", "position": "RB", "team": "MIA", "active": True},
    "5": {"full_name": "Echo WR", "position": "WR", "team": "KC", "active": True},
    "6": {"full_name": "Foxtrot WR", "position": "WR", "team": "BUF", "active": True},
    "7": {"full_name": "Golf WR", "position": "WR", "team": "DET", "active": True, "injury_status": "Out"},
    "8": {"full_name": "Hotel TE", "position": "TE", "team": "MIA", "active": True},
    "9": {"full_name": "India K", "position": "K", "team": "LAR", "active": True},
    "10": {"full_name": "Juliet WR", "position": "WR", "team": "DET", "active": True},
    "KC": {"first_name": "Kansas City", "last_name": "Chiefs", "position": "DEF", "team": "KC", "active": True},
    # free agents
    "20": {"full_name": "Waiver Stud", "position": "RB", "team": "DET", "active": True},
    "21": {"full_name": "Meh WR", "position": "WR", "team": "MIA", "active": True},
}
PROJ = {"1": 20, "2": 15, "3": 14, "4": 6, "5": 13, "6": 11, "7": 16, "8": 8, "9": 8, "10": 7, "KC": 7, "20": 16, "21": 5}

SCORING = {"rec": 0.5, "pass_yd": 0.04, "rush_yd": 0.1}


def proj_line(pts):  # encode points as rushing yards so league scoring reproduces them
    return {"rush_yd": pts * 10, "pts_half_ppr": pts, "pts_ppr": pts, "gp": 1}


@pytest.fixture
def fake_world(monkeypatch):
    lg = {"league_id": "L1", "name": "Test League", "scoring_settings": SCORING,
          "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN", "BN"],
          "settings": {"waiver_type": 2, "waiver_budget": 100}}
    users = [{"user_id": "u1", "display_name": "brendan", "metadata": {"team_name": "Brendobendo"}},
             {"user_id": "u2", "display_name": "rival", "metadata": {"team_name": "Rivals"}}]
    rosters = [{"owner_id": "u1", "players": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "KC"],
                "starters": ["1", "2", "4", "5", "6", "8", "3", "9", "KC"],
                "settings": {"waiver_budget_used": 40}},
               {"owner_id": "u2", "players": [], "starters": []}]
    monkeypatch.setattr(sleeper, "nfl_state", lambda: {"season": "2026", "week": 5})
    monkeypatch.setattr(sleeper, "get_user", lambda u: {"user_id": "u1"})
    monkeypatch.setattr(sleeper, "user_leagues", lambda uid, s: [lg])
    monkeypatch.setattr(sleeper, "league_users", lambda lid: users)
    monkeypatch.setattr(sleeper, "league_rosters", lambda lid: rosters)
    monkeypatch.setattr(sleeper, "all_players", lambda: PLAYERS)
    monkeypatch.setattr(sleeper, "projections", lambda s, w: {k: proj_line(v) for k, v in PROJ.items()})
    monkeypatch.setattr(sleeper, "stats", lambda s, w: {k: proj_line(v) for k, v in PROJ.items()})
    monkeypatch.setattr(sleeper, "trending_adds", lambda: {"20": 25000})

    sched = pd.DataFrame([
        dict(game_id="g1", season=2026, game_type="REG", week=5, gameday="2026-10-04", gametime="13:00",
             away_team="KC", home_team="BUF", location="Home", total_line=50.0, spread_line=3.0,
             roof="outdoors", stadium="Highmark"),
        dict(game_id="g2", season=2026, game_type="REG", week=5, gameday="2026-10-04", gametime="16:25",
             away_team="LA", home_team="DET", location="Home", total_line=44.0, spread_line=-2.0,
             roof="dome", stadium="Ford Field"),
    ])  # MIA is on bye
    stats = pd.DataFrame([
        dict(season_type="REG", week=w, position=pos, opponent_team=opp, fantasy_points=fp, receptions=0)
        for w in (1, 2, 3, 4) for opp, fp in (("BUF", 25), ("KC", 10), ("DET", 18), ("LA", 18))
        for pos in ("RB", "WR", "QB")
    ])
    monkeypatch.setattr(nflverse, "schedule", lambda s: sched)
    monkeypatch.setattr(nflverse, "weekly_stats", lambda s: stats)
    monkeypatch.setattr(weather, "forecast", lambda g: {"indoor": False, "wind_mph": 25, "temp_f": 50,
                                                        "precip_in_hr": 0, "snow_in_hr": 0, "summary": "50F, wind 25mph"}
                        if g["home_team"] == "BUF" else {"indoor": True, "summary": "Indoors"})

    def fake_articles(self, name):
        if name == "Bravo RB":
            return [news.Article("Bravo RB ruled out with torn hamstring", "", "x", "http://a", NOW)]
        if name == "Echo WR":
            return [news.Article("Echo WR expected to play, had a career-high day", "Echo WR looks explosive.",
                                 "x", "http://b", NOW - timedelta(days=1))]
        return []
    monkeypatch.setattr(news.NewsScorer, "player_articles", fake_articles)
    monkeypatch.setattr(news.NewsScorer, "generic_articles", lambda self: [])


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
