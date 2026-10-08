"""Scout artifact data file (offline, fixture data)."""
import json

import pandas as pd

from ffa import nflverse, pipeline, scout, snapshot


def stat(week, pid, name, pos, team, opp, fp, rec=0, **kw):
    home, away = (team, opp) if week % 2 else (opp, team)
    return dict(player_id=pid, player_display_name=name, position=pos, team=team, opponent_team=opp,
                game_id=f"2026_{week:02d}_{away}_{home}", week=week, season_type="REG",
                fantasy_points=fp, receptions=rec, **kw)


def fake_nflverse(monkeypatch):
    rows = [stat(w, "00-1", "Ace Receiver Jr.", "WR", "KC", "BUF", fp, rec=5, targets=9, target_share=0.3)
            for w, fp in ((1, 10), (2, 12), (3, 20), (4, 24))]
    rows += [stat(w, "00-2", "Bo Back", "RB", "BUF", "KC", 15, rec=2, carries=18) for w in (1, 2, 3, 4)]
    rows += [stat(4, "00-3", "Cy Bye", "WR", "MIA", "DET", 8, rec=3)]
    sched = pd.DataFrame([
        dict(game_id=f"g{w}", season=2026, game_type="REG", week=w, gameday=f"2026-09-{6 + 7 * w:02d}", gametime="13:00",
             away_team="KC", home_team="BUF", location="Home", total_line=48.0, spread_line=2.0, home_score=20, away_score=17)
        for w in (1, 2, 3, 4)] + [
        dict(game_id="g5", season=2026, game_type="REG", week=5, gameday="2026-10-11", gametime="16:25",
             away_team="KC", home_team="BUF", location="Home", total_line=50.0, spread_line=3.0, home_score=None, away_score=None),
        dict(game_id="g6", season=2026, game_type="REG", week=5, gameday="2026-10-11", gametime="13:00",
             away_team="DET", home_team="LA", location="Home", total_line=44.0, spread_line=-1.0, home_score=None, away_score=None)])
    inj = pd.DataFrame([dict(season=2026, week=5, gsis_id="00-2", report_status="Questionable",
                             practice_status="Limited Participation in Practice", report_primary_injury="Ankle")])
    monkeypatch.setattr(nflverse, "weekly_stats", lambda s: pd.DataFrame(rows))
    monkeypatch.setattr(nflverse, "schedule", lambda s: sched)
    monkeypatch.setattr(nflverse, "injuries", lambda s: inj)


def test_scout_data(monkeypatch, tmp_path):
    fake_nflverse(monkeypatch)
    cfg = pipeline.load_config()
    data = scout.build(2026, cfg)
    by = {p["id"]: p for p in data["players"]}
    m = data["meta"]
    assert m["through_week"] == 4 and m["next_week"] == 5 and m["model_run"] is None

    ace = by["00-1"]
    assert [w["pts"] for w in ace["weeks"]] == [15, 17, 25, 29]               # fantasy_points + 1.0 PPR x 5 rec
    assert ace["weeks"][0]["opp"] == "vs BUF" and ace["weeks"][1]["opp"] == "@ BUF"   # odd weeks KC hosts
    assert ace["weeks"][0]["tgt"] == 9 and ace["weeks"][0]["tshare"] == 0.3
    assert ace["avg"] == 21.5 and ace["recent"] == round((17 + 25 + 29) / 3, 2)
    pr = ace["proj"]
    assert pr["opp"] == "@ BUF" and pr["base"] == round(0.6 * ace["recent"] + 0.4 * ace["avg"], 2)
    assert abs(pr["value"] - pr["base"] * pr["mult_matchup"] * pr["mult_injury"]) < 0.02 and pr["pos_rank"] == 1

    bo = by["00-2"]
    assert bo["injury"]["status"] == "Questionable" and bo["proj"]["mult_injury"] == 0.85
    assert by["00-3"]["proj"]["bye"] and by["00-3"]["proj"]["value"] == 0            # MIA has no week-5 game
    assert data["teams"]["MIA"]["bye"] == [1, 2, 3, 4, 5] and data["teams"]["KC"]["next_opp"] == "@ BUF"


def test_scout_merges_latest_run(monkeypatch, tmp_path):
    fake_nflverse(monkeypatch)
    snap = {"meta": {"season": 2026, "week": 5, "generated_at": "2026-10-08T12:00:00+00:00", "team": "Brendobendo",
                     "options": {"news": True}, "rec_value": 0.5},
            "players": {"s1": {"name": "Ace Receiver", "position": "WR", "group": "roster", "adj": 18.2, "proj": 17.0,
                               "sentiment": 0.4, "n_articles": 2, "mult_sentiment": 1.03, "notes": [],
                               "articles": [{"title": "Ace breaks out", "link": "https://x", "score": 0.8,
                                             "source": "x", "published": None}]}}}
    (tmp_path / "latest_run.json").write_text(json.dumps(snap))
    out = scout.run(2026, pipeline.load_config(), tmp_path / "scout" / "data.json", tmp_path)
    data = json.loads(out.read_text())
    ace = next(p for p in data["players"] if p["id"] == "00-1")
    assert ace["mine"] and ace["model"]["adj"] == 18.2 and ace["model"]["articles"][0]["title"] == "Ace breaks out"
    assert ace["weeks"][0]["pts"] == 12.5                                        # league's 0.5 PPR from the run
    assert data["meta"]["model_run"]["team"] == "Brendobendo" and len(data["meta"]["sources"]) == 2
    assert snapshot.load(tmp_path)["meta"]["week"] == 5
