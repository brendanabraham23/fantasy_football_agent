"""Web UI API tests (offline): FastAPI TestClient over the fake_world fixture."""
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from conftest import PROJ, proj_line
from ffa import nflverse, pipeline, server, sleeper, snapshot


@pytest.fixture
def client(fake_world, monkeypatch, tmp_path):
    sched = nflverse.schedule(2026)
    past = pd.DataFrame([dict(game_id="g0", season=2026, game_type="REG", week=4, gameday="2026-09-27",
                              gametime="13:00", away_team="KC", home_team="DET", location="Home",
                              total_line=47.0, spread_line=1.0, roof="dome", stadium="Ford Field")])
    full = pd.concat([sched, past], ignore_index=True)
    monkeypatch.setattr(nflverse, "schedule", lambda s: full)
    app = server.create_app("brendan", "Brendobendo", out_dir=tmp_path)
    c = TestClient(app)
    c.tmp_path = tmp_path
    return c


def run_pipeline(c, **opts):
    r = c.post("/api/run", json=opts or {"news": True, "weather": True})
    assert r.status_code == 202 and r.json()["state"] == "running"
    c.app.state.job.thread.join(timeout=30)
    status = c.get("/api/run").json()
    assert status["state"] == "done", status
    return status


def test_summary_requires_a_run_then_reflects_it(client):
    assert client.get("/api/summary").status_code == 404
    assert client.get("/api/news").status_code == 404
    status = run_pipeline(client)
    assert any("Season 2026, week 5" in line for line in status["log"])

    s = client.get("/api/summary").json()
    assert s["meta"]["team"] == "Brendobendo" and s["meta"]["week"] == 5
    assert s["meta"]["options"] == {"news": True, "weather": True}
    assert "4" in s["changes"]["bench"]                       # Delta RB is on bye
    assert {"4", "7"} <= set(s["alerts"])                     # bye + Out
    assert s["waiver_recs"][0]["player_id"] == "20"
    assert s["players"]["20"]["group"] == "candidate" and s["players"]["2"]["group"] == "roster"
    assert s["total"] == round(sum(s["players"][l["player_id"]]["adj"] for l in s["lineup"] if l["player_id"]), 2)
    assert s["stale"] is False and s["current_week"] == 5
    assert (client.tmp_path / "latest_run.json").exists() and (client.tmp_path / "week05_2026_report.md").exists()
    runs = sorted((client.tmp_path / "archive").iterdir())
    assert len(runs) == 1 and runs[0].name.endswith("_week05_2026")
    assert {f.name for f in runs[0].iterdir()} == {"run.json", "week05_2026_report.md", "week05_2026_players.csv"}

    run_pipeline(client, news=False, weather=True)            # a second run is archived alongside, not over
    runs = sorted((client.tmp_path / "archive").iterdir())
    assert len(runs) == 2
    assert [json.loads((r / "run.json").read_text())["meta"]["options"]["news"] for r in runs] == [True, False]


def test_second_run_while_running_is_rejected(client):
    client.app.state.job.state = "running"
    assert client.post("/api/run", json={}).status_code == 409


def test_run_failure_is_reported(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sleeper down")
    monkeypatch.setattr(pipeline, "run", boom)
    client.post("/api/run", json={})
    client.app.state.job.thread.join(timeout=30)
    status = client.get("/api/run").json()
    assert status["state"] == "error" and "sleeper down" in status["error"]


def test_search(client):
    assert client.get("/api/players/search?q=").json() == []
    hits = client.get("/api/players/search?q=rb").json()
    by = {h["player_id"]: h for h in hits}
    assert {"2", "3", "4"} <= set(by) and "20" not in by
    assert by["2"]["owner"] == "Mine"
    assert by["3"]["team"] == "LA"
    stud = client.get("/api/players/search?q=stud").json()[0]
    assert stud["name"] == "Waiver Stud" and stud["owner"] is None
    assert client.get("/api/players/search?q=chiefs").json()[0]["position"] == "DEF"


def test_player_detail_live_then_snapshot(client):
    d = client.get("/api/players/5").json()                  # Echo WR, KC
    assert d["eval_source"] == "live" and d["owner"] == "Mine"
    h = {x["week"]: x for x in d["history"]}
    assert len(h) == 5 and h[5]["current"] and h[5]["actual"] is None and h[5]["projected"] == 13
    assert h[4]["opp"] == "@ DET" and h[4]["actual"] == 13 and h[4]["stats"]["rush_yd"] == 130
    assert h[3]["bye"] and h[3]["opp"] == "BYE"
    assert d["games"] == 4 and d["season_pts"] == 52

    run_pipeline(client)
    d = client.get("/api/players/5").json()
    assert d["eval_source"] == "snapshot" and d["eval"]["sentiment"] > 0 and d["eval"]["articles"]
    assert client.get("/api/players/nope").status_code == 404


def test_waiver_pool_and_whatif(client):
    pool = client.get("/api/waivers/pool").json()
    ids = {p["player_id"] for p in pool["players"]}
    assert {"20", "21"} <= ids and not ids & {"1", "2", "5", "KC"}
    stud = next(p for p in pool["players"] if p["player_id"] == "20")
    assert stud["season_pts"] == 64 and stud["ppg"] == 16 and stud["trending_adds"] == 25000

    run_pipeline(client)
    rec = client.get("/api/summary").json()["waiver_recs"][0]
    w = client.get("/api/waivers/whatif/20").json()
    assert w["weekly_gain"] == rec["weekly_gain"] and w["ros_gain"] == rec["ros_gain"] and w["bid"] == rec["bid"]
    assert w["drop"]["player_id"] == rec["drop_id"]
    assert next(p for p in client.get("/api/waivers/pool").json()["players"] if p["player_id"] == "20")["recommended"]
    assert client.get("/api/waivers/whatif/2").status_code == 400
    assert client.get("/api/waivers/whatif/nope").status_code == 404


def test_news(client):
    run_pipeline(client)
    n = client.get("/api/news").json()
    by = {p["player_id"]: p for p in n["players"]}
    assert by["2"]["sentiment"] < 0 and by["2"]["articles"][0]["title"].startswith("Bravo RB ruled out")
    assert abs(n["players"][0]["sentiment"]) >= abs(n["players"][-1]["sentiment"])
    live = client.get("/api/news/5").json()
    assert live["sentiment"] > 0 and live["n_articles"] == 1 and live["mult_sentiment"] > 1
    assert live["articles"][0]["source"] == "x"


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Run pipeline" in r.text
    assert client.get("/app.js").status_code == 200


def test_snapshot_round_trip(fake_world):
    res = pipeline.run("brendan", "Brendobendo")
    data = json.loads(json.dumps(snapshot.to_dict(res, ["[warn] x"]), default=str))
    assert data["warnings"] == ["[warn] x"]
    e = next(e for e in res.roster if e.name == "Echo WR")
    back = snapshot.player_from_dict(data["players"][e.player_id])
    assert back.adj == e.adj and back.mult_weather == e.mult_weather and back.matchup_grade == e.matchup_grade


def test_capture_collects_warnings():
    with snapshot.capture() as lines:
        print("  - progress")
        print("[warn] feed failed")
    assert lines == ["  - progress", "[warn] feed failed"]
    assert snapshot.warnings_from(lines) == ["[warn] feed failed"]


def test_archived_runs_list_load_and_compare(client):
    assert client.get("/api/runs").json() == []
    run_pipeline(client, news=True, weather=True)
    run_pipeline(client, news=False, weather=True)
    runs = client.get("/api/runs").json()
    assert len(runs) == 2 and runs[0]["latest"] and not runs[1]["latest"]
    assert runs[0]["options"]["news"] is False and runs[1]["options"]["news"] is True
    assert runs[1]["week"] == 5 and runs[1]["total"] > 0

    old_id = runs[1]["id"]
    old = client.get(f"/api/summary?run={old_id}").json()
    assert old["archived"] and old["run_id"] == old_id and old["meta"]["options"]["news"] is True
    assert client.get("/api/summary").json()["meta"]["options"]["news"] is False
    assert client.get(f"/api/news?run={old_id}").json()["news_enabled"] is True

    c = client.get(f"/api/compare?run={old_id}").json()
    assert c["same_week"] and c["total_delta"] == round(c["new"]["total"] - c["old"]["total"], 2)
    bravo = next(p for p in c["players"] if p["player_id"] == "2")
    assert bravo["status"] == "kept" and bravo["delta"] > 0      # negative news no longer applied
    assert {p["status"] for p in c["players"]} == {"kept"}
    assert [r["player_id"] for r in c["recs_kept"]] == ["20"]

    assert client.get("/api/summary?run=nope").status_code == 404
    assert client.get("/api/summary?run=../latest_run.json").status_code == 404
    assert client.get("/api/compare?run=nope").status_code == 404


def test_compare_roster_moves():
    def snap(players, lineup, recs, total):
        return {"meta": {"generated_at": "x", "week": 5, "season": 2026}, "total": total, "lineup": lineup,
                "players": players, "waiver_recs": [{"player_id": r} for r in recs]}
    p = lambda name, adj, group="roster": {"name": name, "position": "RB", "adj": adj, "group": group}
    old = snap({"a": p("A", 10), "b": p("B", 5), "c": p("C", 9, "candidate")}, [{"slot": "RB", "player_id": "a"}], ["c"], 10)
    new = snap({"a": p("A", 12), "c": p("C", 9)}, [{"slot": "RB", "player_id": "c"}], ["d"], 9)
    c = snapshot.compare(old, new)
    by = {r["player_id"]: r for r in c["players"]}
    assert by["a"]["delta"] == 2 and by["a"]["slot_old"] == "RB" and by["a"]["slot_new"] == "BN"
    assert by["b"]["status"] == "dropped" and by["b"]["adj_new"] is None
    assert by["c"]["status"] == "added" and by["c"]["adj_old"] is None and by["c"]["slot_new"] == "RB"
    assert c["total_delta"] == -1 and [r["player_id"] for r in c["recs_gone"]] == ["c"]


@pytest.fixture
def week4_archive(fake_world, monkeypatch, tmp_path):
    """An archived week-4 run, reviewed from week 5. Week-5 stats are absurd (999) so any leak is obvious."""
    sched = nflverse.schedule(2026)
    wk4 = sched.assign(week=4, game_id=sched["game_id"] + "_4", gameday="2026-09-27")
    full = pd.concat([sched, wk4], ignore_index=True)
    monkeypatch.setattr(nflverse, "schedule", lambda s: full)
    monkeypatch.setattr(sleeper, "stats", lambda s, w: {k: proj_line(999 if w >= 5 else v + w) for k, v in PROJ.items()})
    mine4 = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "20", "KC"]      # held Waiver Stud, not Juliet WR
    starters4 = ["1", "2", "3", "5", "6", "8", "20", "9", "KC"]
    monkeypatch.setattr(sleeper, "matchups", lambda lid, w: [
        {"roster_id": 1, "players": mine4, "starters": starters4,
         "players_points": {pid: float(PROJ[pid] + 4) for pid in mine4}},
        {"roster_id": 2, "players": ["21"], "starters": ["21"], "players_points": {"21": 9.0}},
    ] if w == 4 else [])
    app4 = server.create_app("brendan", "Brendobendo", week=4, out_dir=tmp_path)
    TestClient(app4).post("/api/run", json={})
    app4.state.job.thread.join(timeout=30)
    assert app4.state.job.state == "done", app4.state.job.error
    c = TestClient(server.create_app("brendan", "Brendobendo", out_dir=tmp_path))   # "now" is week 5
    run = c.get("/api/runs").json()[0]
    assert run["week"] == 4
    c.run_id = run["id"]
    return c


def no_999(obj):
    return "999" not in json.dumps(obj)


def test_as_of_player_detail_stops_at_run_week(week4_archive):
    c, r = week4_archive, week4_archive.run_id
    d = c.get(f"/api/players/5?run={r}").json()
    assert d["as_of"] and d["week"] == 4 and d["through"] == 4
    assert [h["week"] for h in d["history"]] == [1, 2, 3, 4] and no_999(d)
    assert d["history"][3]["actual"] == 17 and d["actual"] == 17           # week-4 outcome from Sleeper matchups
    assert d["games"] == 3 and d["season_pts"] == 13 * 3 + 6              # pre-week totals only (weeks 1-3)
    assert d["eval_source"] == "snapshot" and d["owner"] == "Mine"
    charlie = c.get(f"/api/players/3?run={r}").json()
    assert charlie["injury_status"] is None                               # today's designation would leak
    assert c.get("/api/players/5").json()["as_of"] is False               # latest view unchanged


def test_as_of_waiver_pool_uses_that_weeks_rosters(week4_archive):
    c, r = week4_archive, week4_archive.run_id
    pool = c.get(f"/api/waivers/pool?run={r}").json()
    ids = {p["player_id"] for p in pool["players"]}
    assert pool["as_of"] and pool["week"] == 4 and no_999(pool)
    assert "10" in ids and not ids & {"20", "21"}                          # Juliet was a FA in week 4
    juliet = next(p for p in pool["players"] if p["player_id"] == "10")
    assert juliet["actual"] == 11 and juliet["season_pts"] == 7 * 3 + 6 and juliet["trending_adds"] == 0
    assert c.get(f"/api/waivers/whatif/10?run={r}").status_code == 200
    assert c.get(f"/api/waivers/whatif/20?run={r}").status_code == 400
    assert c.get(f"/api/players/search?q=stud&run={r}").json()[0]["owner"] == "Mine"


def test_archived_summary_scores_predictions_against_outcomes(week4_archive):
    c, r = week4_archive, week4_archive.run_id
    s = c.get(f"/api/summary?run={r}").json()
    o = s["outcome"]
    assert o["week"] == 4 and no_999(o)
    rec = [row["player_id"] for row in s["lineup"] if row["player_id"]]
    assert o["lineup"]["recommended"] == round(sum(o["actual"][p] or 0 for p in rec), 2)
    assert o["lineup"]["played_ids"] == ["1", "2", "3", "5", "6", "8", "20", "9", "KC"]
    assert o["lineup"]["best"] >= max(o["lineup"]["recommended"], o["lineup"]["played"])
    assert o["errors"]["n"] > 0 and o["errors"]["mae"] >= 0
    assert c.get("/api/summary").json()["outcome"] is None
