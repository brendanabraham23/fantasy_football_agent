"""Serialize a pipeline run to JSON for the web UI, plus stdout capture for run logs/warnings."""
from __future__ import annotations

import io
import json
import shutil
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import Result
from .ranker import PlayerEval

LATEST = "latest_run.json"


class _Tee(io.TextIOBase):
    """Writes through to the real stream and records complete lines."""

    def __init__(self, stream, lines: list[str]):
        self.stream, self.lines, self._buf = stream, lines, ""
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        self.stream.write(s)
        with self._lock:
            self._buf += s
            *done, self._buf = self._buf.split("\n")
            self.lines.extend(line for line in done if line.strip())
        return len(s)

    def flush(self):
        self.stream.flush()


@contextmanager
def capture(lines: list[str] | None = None):
    """Tee stdout into `lines` (yielded). `[warn]` lines become the snapshot's warnings."""
    lines = [] if lines is None else lines
    old = sys.stdout
    sys.stdout = _Tee(old, lines)
    try:
        yield lines
    finally:
        sys.stdout = old


def warnings_from(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip().startswith("[warn]")]


def player_dict(e: PlayerEval, group: str | None = None) -> dict:
    d = asdict(e)
    d["headlines"] = [list(h) for h in e.headlines]
    d.update(matchup_grade=e.matchup_grade, ros_value=round(e.ros_value, 2))
    if group:
        d["group"] = group
    return d


def player_from_dict(d: dict) -> PlayerEval:
    names = {f.name for f in fields(PlayerEval)}
    return PlayerEval(**{k: v for k, v in d.items() if k in names})


def to_dict(res: Result, warnings: list[str] | None = None, options: dict | None = None) -> dict:
    c = res.ctx
    players = {}
    for group, evals in (("candidate", res.candidates), ("reserve", res.reserve), ("roster", res.roster)):
        for e in evals:
            players[e.player_id] = player_dict(e, group)

    optimal = [e.player_id for _, e in res.lineup if e]
    current = [p for p in res.current_starters if p and p != "0"]
    known = {e.player_id for e in res.roster + res.reserve}
    settings = c.league.get("settings") or {}
    rs = c.my_roster.get("settings") or {}
    faab = settings.get("waiver_type") == 2
    return {
        "meta": {
            "team": c.my_team_name, "league": c.league.get("name"), "league_id": c.league.get("league_id"),
            "season": c.season, "week": c.week,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "options": options or {}, "roster_positions": c.league.get("roster_positions", []),
            "faab": faab,
            "budget_left": (settings.get("waiver_budget") or 0) - (rs.get("waiver_budget_used") or 0) if faab else None,
            "sentiment_weight": c.cfg.get("sentiment_weight"), "weights": c.cfg.get("weights"),
        },
        "lineup": [{"slot": slot, "player_id": e.player_id if e else None} for slot, e in res.lineup],
        "current_starters": current,
        "players": players,
        "waiver_recs": [{"player_id": r.player.player_id, "weekly_gain": r.weekly_gain, "ros_gain": r.ros_gain,
                         "drop_id": r.drop.player_id if r.drop else None, "bid": r.bid, "score": r.score,
                         "bid_detail": r.bid_detail}
                        for r in res.waiver_recs],
        "changes": {"start": [p for p in optimal if p not in current and p in known],
                    "bench": [p for p in current if p not in optimal and p in known]},
        "alerts": [e.player_id for e in res.roster if e.on_bye or e.mult_injury < 1],
        "total": round(sum(e.adj for _, e in res.lineup if e), 2),
        "warnings": warnings or [],
    }


def save(data: dict, out_dir: str | Path, files: tuple[Path, ...] = (), archive_dir: str = "archive") -> Path:
    """Write latest_run.json and archive this run (snapshot + `files`) under archive/<timestamp>_weekNN/."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, default=str)
    m = data["meta"]
    stamp = datetime.fromisoformat(m["generated_at"]).astimezone().strftime("%Y-%m-%d_%H%M%S")
    base = out / archive_dir / f"{stamp}_week{m['week']:02d}_{m['season']}"
    run_dir, n = base, 1
    while run_dir.exists():
        n += 1
        run_dir = base.with_name(f"{base.name}-{n}")
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(text)
    for f in files:
        shutil.copy2(f, run_dir / Path(f).name)
    path = out / LATEST
    path.write_text(text)
    return run_dir


def load(out_dir: str | Path) -> dict | None:
    path = Path(out_dir) / LATEST
    return json.loads(path.read_text()) if path.exists() else None


# ---- archive -------------------------------------------------------------------

_meta_cache: dict[Path, tuple[float, dict]] = {}


def _run_summary(run_dir: Path) -> dict | None:
    path = run_dir / "run.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _meta_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    m = data["meta"]
    row = {"id": run_dir.name, "generated_at": m["generated_at"], "season": m["season"], "week": m["week"],
           "team": m.get("team"), "total": data.get("total"), "options": m.get("options", {}),
           "n_changes": len(data.get("changes", {}).get("start", [])), "n_recs": len(data.get("waiver_recs", []))}
    _meta_cache[path] = (mtime, row)
    return row


def list_runs(out_dir: str | Path, archive_dir: str = "archive") -> list[dict]:
    """Archived runs, newest first. The one matching latest_run.json is flagged `latest`."""
    root = Path(out_dir) / archive_dir
    rows = [r for d in root.iterdir() if d.is_dir() and (r := _run_summary(d))] if root.is_dir() else []
    rows.sort(key=lambda r: (r["generated_at"], r["id"]), reverse=True)  # "-2" suffix sorts after its base
    latest = load(out_dir)
    gen = latest["meta"]["generated_at"] if latest else None
    first = next((i for i, r in enumerate(rows) if r["generated_at"] == gen), None)
    return [{**r, "latest": i == first} for i, r in enumerate(rows)]


def load_run(out_dir: str | Path, run_id: str, archive_dir: str = "archive") -> dict | None:
    root = Path(out_dir) / archive_dir
    if not root.is_dir() or run_id not in {d.name for d in root.iterdir() if d.is_dir()}:
        return None
    path = root / run_id / "run.json"
    return json.loads(path.read_text()) if path.exists() else None


def _slots(data: dict) -> dict[str, str]:
    return {row["player_id"]: row["slot"] for row in data["lineup"] if row["player_id"]}


def compare(old: dict, new: dict) -> dict:
    """What changed between two snapshots: lineup total, per-player adjusted points/slots, waiver targets."""
    po, pn = old["players"], new["players"]
    so, sn = _slots(old), _slots(new)
    mine = lambda d: {pid for pid, e in d.items() if e.get("group") in ("roster", "reserve")}
    players = []
    for pid in mine(po) | mine(pn):
        a, b = po.get(pid), pn.get(pid)
        on_old, on_new = a is not None and a.get("group") != "candidate", b is not None and b.get("group") != "candidate"
        e = b or a
        adj_old = a["adj"] if on_old else None
        adj_new = b["adj"] if on_new else None
        players.append({
            "player_id": pid, "name": e["name"], "position": e["position"],
            "adj_old": adj_old, "adj_new": adj_new,
            "delta": round(adj_new - adj_old, 2) if adj_old is not None and adj_new is not None else None,
            "slot_old": so.get(pid, "BN" if on_old else None), "slot_new": sn.get(pid, "BN" if on_new else None),
            "status": "added" if not on_old else "dropped" if not on_new else "kept",
        })
    players.sort(key=lambda r: (r["status"] != "kept", -abs(r["delta"] or 0)))
    ro = [r["player_id"] for r in old["waiver_recs"]]
    rn = [r["player_id"] for r in new["waiver_recs"]]
    name = lambda pid: (pn.get(pid) or po.get(pid) or {}).get("name", pid)
    return {
        "old": {"generated_at": old["meta"]["generated_at"], "week": old["meta"]["week"], "total": old["total"]},
        "new": {"generated_at": new["meta"]["generated_at"], "week": new["meta"]["week"], "total": new["total"]},
        "total_delta": round(new["total"] - old["total"], 2),
        "same_week": old["meta"]["week"] == new["meta"]["week"] and old["meta"]["season"] == new["meta"]["season"],
        "players": players,
        "recs_new": [{"player_id": p, "name": name(p)} for p in rn if p not in ro],
        "recs_gone": [{"player_id": p, "name": name(p)} for p in ro if p not in rn],
        "recs_kept": [{"player_id": p, "name": name(p)} for p in rn if p in ro],
    }
