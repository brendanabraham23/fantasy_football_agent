"""Hosted mode (offline): data-branch sync from a local git repo, GitHub Actions runs faked, Basic auth."""
import base64
import subprocess

import pytest
from fastapi.testclient import TestClient

from ffa import hosting, pipeline, server, snapshot


def sh(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def publish(origin, src, data, msg):
    """Commit `data` as reports/latest_run.json on the origin's data branch (what predict.yml does)."""
    snapshot.save(data, src / "reports")
    sh("add", "-A", cwd=src)
    sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, cwd=src)
    sh("push", "-q", "origin", "HEAD:data", cwd=src)


@pytest.fixture
def world(fake_world, tmp_path):
    res = pipeline.run("brendan", "Brendobendo", None, None, None, pipeline.load_config(), news=False, wx=False)
    data = snapshot.to_dict(res, [], {"news": False, "weather": False})
    origin, src = tmp_path / "origin.git", tmp_path / "src"
    sh("init", "-q", "--bare", str(origin), cwd=tmp_path)
    sh("init", "-q", str(src), cwd=tmp_path)
    sh("remote", "add", "origin", str(origin), cwd=src)
    publish(origin, src, data, "run 1")
    return {"origin": origin, "src": src, "data": data, "tmp": tmp_path}


def make(world, token="t0k", password=None):
    h = hosting.Hosting(repo="me/ffa", clone_dir=world["tmp"] / "clone", token=token, ref="agent_v2",
                        sync_minutes=60, remote_url=str(world["origin"]))
    cfg = pipeline.load_config()
    cfg["ui"]["gh_poll_seconds"] = 0  # no status caching in tests
    app = server.create_app("brendan", "Brendobendo", cfg=cfg, hosting=h, password=password)
    return TestClient(app), h


def test_sync_reads_data_branch_and_picks_up_new_runs(world):
    c, h = make(world)
    s = c.get("/api/summary").json()
    assert s["meta"]["team"] == world["data"]["meta"]["team"]
    newer = {**world["data"], "meta": {**world["data"]["meta"], "generated_at": "2099-01-01T00:00:00+00:00"},
             "total": 123.45}
    publish(world["origin"], world["src"], newer, "run 2")
    assert h.sync()
    assert c.get("/api/summary").json()["total"] == 123.45
    assert len(c.get("/api/runs").json()) == 2


def test_sync_failure_is_a_warning_not_a_crash(world, capsys):
    h = hosting.Hosting(repo="me/ffa", clone_dir=world["tmp"] / "x", remote_url=str(world["tmp"] / "nope.git"))
    assert not h.sync() and h.last_error
    assert "[warn]" in capsys.readouterr().out


def test_run_dispatches_workflow_and_syncs_when_it_succeeds(world, monkeypatch):
    calls, runs = [], []

    def fake(method, url, token, body=None):
        calls.append((method, url, body))
        if url.endswith("/dispatches"):
            runs.append({"id": 2, "status": "queued", "conclusion": None, "html_url": "https://gh/run/2",
                         "created_at": "2999-01-01T00:00:00Z", "updated_at": "2999-01-01T00:00:00Z"})
            return None
        return {"workflow_runs": runs[-1:]}

    monkeypatch.setattr(hosting, "gh_request", fake)
    runs.append({"id": 1, "status": "completed", "conclusion": "success", "html_url": "https://gh/run/1",
                 "created_at": "2000-01-01T00:00:00Z", "updated_at": "2000-01-01T00:05:00Z"})
    c, h = make(world)
    synced = []
    monkeypatch.setattr(h, "sync", lambda: synced.append(1) or True)

    r = c.post("/api/run", json={"news": False, "weather": True})
    assert r.status_code == 202 and r.json()["state"] == "running"
    method, url, body = next(x for x in calls if x[1].endswith("/dispatches"))
    assert url.endswith("/repos/me/ffa/actions/workflows/predict.yml/dispatches")
    assert body == {"ref": "agent_v2", "inputs": {"news": "false", "weather": "true"}}
    assert c.post("/api/run").status_code == 409

    runs[-1] = {**runs[-1], "status": "completed", "conclusion": "success", "updated_at": "2999-01-01T00:04:00Z"}
    st = c.get("/api/run").json()
    assert st["state"] == "done" and synced


def test_failed_workflow_is_reported(world, monkeypatch):
    monkeypatch.setattr(hosting, "gh_request", lambda m, u, t, b=None: {"workflow_runs": [
        {"id": 9, "status": "completed", "conclusion": "failure", "html_url": "https://gh/run/9",
         "created_at": "2000-01-01T00:00:00Z", "updated_at": "2000-01-01T00:01:00Z"}]})
    c, _ = make(world)
    st = c.get("/api/run").json()
    assert st["state"] == "error" and "https://gh/run/9" in st["error"]


def test_without_token_runs_are_disabled(world, monkeypatch):
    monkeypatch.setattr(hosting, "gh_request", lambda *a, **k: {"workflow_runs": []})
    c, _ = make(world, token=None)
    assert c.get("/api/config").json()["can_run"] is False
    assert c.post("/api/run").status_code == 403


def test_password(world, monkeypatch):
    monkeypatch.setattr(hosting, "gh_request", lambda *a, **k: {"workflow_runs": []})
    c, _ = make(world, password="hunter2")
    assert c.get("/api/summary").status_code == 401
    assert c.get("/").status_code == 401
    ok = {"Authorization": "Basic " + base64.b64encode(b"me:hunter2").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"me:nope").decode()}
    assert c.get("/api/summary", headers=bad).status_code == 401
    assert c.get("/api/summary", headers=ok).status_code == 200
