"""LLM headline scoring through the Claude Code CLI (`claude -p`), with an on-disk score cache.

Auth is the CLI's own: a local login, or CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) in CI.
Every failure returns None so the caller can fall back to VADER.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading

from . import http

SYSTEM = """You score NFL news headlines for fantasy football. For each headline, output how good or bad the news is
for the named player's fantasy value this week / near term, from -1.0 to +1.0.

Anchors:
-1.0 season-ending injury, suspension, released
-0.7 ruled out, benched, lost starting job
-0.5 job openly in jeopardy, demoted or losing snaps, must-sit, worst outing of career, real injury scare
-0.3 clearly poor performance, struggling, downgraded in rankings
-0.15 mildly negative (held in check, low-volume game, minor concern)
 0   no fantasy signal: stat/bio pages, "who should I start/pick up" polls, roundups, off-field items, quotes,
     trade speculation, or the headline is really about someone else
+0.15 mildly positive
+0.3 solid game or TD, starting job affirmed, waiver-add buzz
+0.5 strong performance, climbs rankings, favorable matchup / streaming pick
+0.7 named starter, role expansion, career-high or breakout game
+1.0 returns from major injury into a full role, huge role windfall

Rules: judge fantasy impact, not tone (a QB scolding a teammate is ~0 for the QB). A team win/loss is ~0 unless the
player's own play is the story. If the headline is mainly about a different player, score its effect on THIS player
(often 0; a teammate's injury can be positive for him). Ignore the trailing " - Source" text."""

SCHEMA = {"type": "object", "properties": {"scores": {"type": "array", "items": {"type": "object",
          "properties": {"id": {"type": "integer"}, "score": {"type": "number"}}, "required": ["id", "score"]}}},
          "required": ["scores"]}

_lock = threading.Lock()
_down = False   # set after the first hard failure so one run doesn't retry a broken CLI for every player


def _cache_file():
    return http.CACHE_DIR / "llm_scores.json"


def load_cache() -> dict[str, float]:
    try:
        return json.loads(_cache_file().read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict[str, float], keep: int) -> None:
    """Newest `keep` entries only (dicts keep insertion order), so the file can't grow without bound."""
    try:
        http.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(dict(list(cache.items())[-keep:]), separators=(",", ":")))
    except OSError as exc:
        print(f"[warn] couldn't save LLM score cache: {exc}")


def key(model: str, player: str, title: str) -> str:
    return "|".join((model, player.lower(), re.sub(r"\W+", "", title.lower())))


def _call(cmd: str, model: str, prompt: str, timeout: int) -> dict:
    p = subprocess.run([cmd, "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                        "--system-prompt", SYSTEM, "--output-format", "json", "--json-schema", json.dumps(SCHEMA)],
                       input=prompt, capture_output=True, text=True, timeout=timeout, cwd=str(http.CACHE_DIR))
    res = json.loads(p.stdout)
    if res.get("is_error"):
        raise RuntimeError(str(res.get("result"))[:200])
    return res.get("structured_output") or json.loads(res["result"])


def score_headlines(player: str, position: str | None, team: str | None, titles: list[str],
                    cfg: dict) -> list[float] | None:
    """One score in [-1, 1] per title, in order, or None if the CLI is missing, unauthenticated or misbehaving."""
    global _down
    cmd = cfg.get("command", "claude")
    if _down or not titles or not shutil.which(cmd):
        return None
    lines = "\n".join(f"{i}\t{t}" for i, t in enumerate(titles))
    prompt = (f"Player: {player} ({position or '?'}, {team or '?'})\n"
              f"Score every headline below (id<TAB>headline). Return all {len(titles)} ids.\n\n{lines}")
    err = "missing ids"
    http.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(1 + cfg.get("retries", 2)):
        try:
            out = _call(cmd, cfg.get("model", "sonnet"), prompt, cfg.get("timeout", 300))
            got = {int(s["id"]): max(-1.0, min(1.0, float(s["score"]))) for s in out["scores"]}
            if all(i in got for i in range(len(titles))):
                return [round(got[i], 2) for i in range(len(titles))]
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:200]
    with _lock:
        if not _down:
            print(f"[warn] LLM news scoring unavailable ({err}); using VADER for the rest of this run")
        _down = True
    return None
