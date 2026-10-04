"""Shared offline fixtures: every network call is replaced with fixture data."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ffa import news, nflverse, sleeper, weather

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
