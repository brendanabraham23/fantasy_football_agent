"""League export for Scout (offline, fake_world)."""
import json

import pytest

from conftest import PLAYERS
from ffa import export, pipeline, report, sleeper, snapshot


@pytest.fixture
def reports(fake_world, tmp_path, monkeypatch):
    players = {**PLAYERS, "1": {**PLAYERS["1"], "gsis_id": "00-0001"}}
    monkeypatch.setattr(sleeper, "all_players", lambda: players)
    monkeypatch.setattr(sleeper, "matchups", lambda lid, w, **kw: [
        {"roster_id": 1, "matchup_id": 1, "starters": ["1", "2", "3", "5", "6", "8", "4", "9", "KC"],
         "players": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "KC"], "players_points": {"1": 12.5}},
        {"roster_id": 2, "matchup_id": 1, "starters": [], "players": [], "players_points": {}}])
    cfg = pipeline.load_config()
    res = pipeline.run("brendan", "Brendobendo", None, None, None, cfg, news=False, wx=False)
    md, csv = report.save(res, tmp_path)
    snapshot.save(snapshot.to_dict(res, [], {"news": False, "weather": False}), tmp_path, (md, csv))
    return tmp_path


def test_export_covers_every_ui_section(reports):
    cfg = pipeline.load_config()
    out = export.run("brendan", "Brendobendo", None, None, cfg, reports / "league.json", reports)
    d = json.loads(out.read_text())
    assert not d["errors"], d["errors"]
    assert d["meta"]["team"] == "Brendobendo" and d["meta"]["week"]
    assert d["summary"]["lineup"] and d["matchup"]["me"]["starters"] and d["roster"]["players"]
    assert len(d["runs"]) == 1 and d["news"] is not None
    pool = d["waivers"]["pool"]["players"]
    assert {r["player_id"] for r in pool} >= {"20", "21"}
    assert set(d["waivers"]["whatif"]) >= {"20"} and "weekly_gain" in d["waivers"]["whatif"]["20"]
    assert {"1", "2", "KC", "20"} <= set(d["players"])            # roster, D/ST and waiver targets have pages
    assert d["players"]["1"]["history"] and d["players"]["1"]["eval"]
    assert d["scout_ids"]["1"] == "00-0001" and d["scout_ids"]["KC"] == "DEF-KC"


def test_export_records_missing_sections_instead_of_failing(fake_world, tmp_path):
    d = export.build("brendan", "Brendobendo", None, None, pipeline.load_config(), tmp_path)
    assert d["summary"] is None and "/api/summary" in d["errors"]
    assert d["matchup"] is not None   # live sections still export without a pipeline run


def test_scout_id_falls_back_to_name_and_position(tmp_path):
    (tmp_path / "scout").mkdir()
    (tmp_path / "scout" / "data.json").write_text(json.dumps({"players": [
        {"id": "00-0099", "name": "Bucky Irving", "pos": "RB"}, {"id": "00-0100", "name": "Jalen Coker", "pos": "WR"}]}))
    by_name = export.scout_names(tmp_path / "scout" / "data.json")
    assert export.scout_id("11584", {"full_name": "Bucky Irving", "position": "RB"}, by_name) == "00-0099"
    assert export.scout_id("x", {"first_name": "Jalen", "last_name": "Coker", "position": "WR"}, by_name) == "00-0100"
    assert export.scout_id("x", {"full_name": "Jalen Coker", "position": "TE"}, by_name) is None   # position must match
    assert export.scout_id("3163", {"full_name": "Jared Goff", "position": "QB", "gsis_id": " 00-0033106"}, by_name) == "00-0033106"
    assert export.scout_names(tmp_path / "missing.json") == {}


def test_export_includes_archived_runs(reports):
    cfg = pipeline.load_config()
    res = pipeline.run("brendan", "Brendobendo", None, None, None, cfg, news=False, wx=False)
    snapshot.save(snapshot.to_dict(res, [], {"news": False, "weather": False}), reports, report.save(res, reports))
    d = export.build("brendan", "Brendobendo", None, None, cfg, reports)
    assert not d["errors"], d["errors"]
    old = [r["id"] for r in d["runs"] if not r["latest"]]
    assert len(old) == 1 and set(d["archived"]) == set(old)
    a = d["archived"][old[0]]
    assert a["summary"]["archived"] and a["summary"]["lineup"] and a["compare"]["new"]["total"] is not None
