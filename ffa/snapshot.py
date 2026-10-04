"""Serialize a pipeline run to JSON for the web UI, plus stdout capture for run logs/warnings."""
from __future__ import annotations

import io
import json
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
                         "drop_id": r.drop.player_id if r.drop else None, "bid": r.bid, "score": r.score}
                        for r in res.waiver_recs],
        "changes": {"start": [p for p in optimal if p not in current and p in known],
                    "bench": [p for p in current if p not in optimal and p in known]},
        "alerts": [e.player_id for e in res.roster if e.on_bye or e.mult_injury < 1],
        "total": round(sum(e.adj for _, e in res.lineup if e), 2),
        "warnings": warnings or [],
    }


def save(data: dict, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, default=str)
    m = data["meta"]
    (out / f"week{m['week']:02d}_{m['season']}_run.json").write_text(text)
    path = out / LATEST
    path.write_text(text)
    return path


def load(out_dir: str | Path) -> dict | None:
    path = Path(out_dir) / LATEST
    return json.loads(path.read_text()) if path.exists() else None
