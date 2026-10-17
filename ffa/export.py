"""Static export of what the web UI shows, for the Scout artifact (which can't call the API or Sleeper).

    python -m ffa export --username <sleeper_username>   # -> reports/league.json

Every section is the JSON a UI endpoint returns, produced by calling the app in-process, so the export and the
UI can't drift apart. On-demand pieces (waiver what-ifs, player pages) are precomputed for the players the UI
would usually show: your roster, your opponent's, the waiver recommendations and the top of the waiver pool.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import server, sleeper


def scout_id(pid: str, p: dict) -> str | None:
    """The Scout data.json id for a Sleeper player: nflverse gsis id, or DEF-<team> for a D/ST."""
    if p.get("position") == "DEF":
        team = sleeper.norm_team(p.get("team") or pid)
        return f"DEF-{team}" if team else None
    return p.get("gsis_id") or None


def build(username: str, team_name: str, league_id: str | None, week: int | None, cfg: dict,
          reports_dir: str | Path = "reports") -> dict:
    from fastapi.testclient import TestClient  # fastapi/httpx are already UI dependencies

    ecfg = cfg.get("export", {})
    client = TestClient(server.create_app(username, team_name, league_id, week, cfg, reports_dir),
                        raise_server_exceptions=False)
    errors: dict[str, str] = {}

    def get(path: str):
        r = client.get(path)
        if r.status_code == 200:
            return r.json()
        try:
            errors[path] = r.json().get("detail") or r.text
        except ValueError:
            errors[path] = r.text
        print(f"[warn] export {path}: {errors[path]}")
        return None

    summary, matchup, roster = get("/api/summary"), get("/api/matchup"), get("/api/live/roster")
    pool = get("/api/waivers/pool")
    pool_rows = (pool or {}).get("players", [])
    recs = [r["player_id"] for r in (summary or {}).get("waiver_recs", [])]
    whatif_ids = list(dict.fromkeys(recs + [r["player_id"] for r in pool_rows[: ecfg.get("whatif_top", 40)]]))
    whatif = {pid: w for pid in whatif_ids if (w := get(f"/api/waivers/whatif/{pid}"))}

    ids: list[str] = []
    for side in ("me", "opponent"):
        t = (matchup or {}).get(side) or {}
        ids += [r["player_id"] for r in (t.get("starters") or []) + (t.get("bench") or []) if r.get("player_id")]
    ids += (roster or {}).get("players", [])
    ids += [pid for pid, d in (summary or {}).get("players", {}).items() if d.get("group") in ("roster", "reserve")]
    ids += whatif_ids
    players = {pid: d for pid in dict.fromkeys(ids) if (d := get(f"/api/players/{pid}"))}

    try:
        sp = sleeper.all_players()
        scout_ids = {pid: sid for pid in set(players) | {r["player_id"] for r in pool_rows}
                     if (sid := scout_id(pid, sp.get(pid) or {}))}
    except Exception as exc:
        print(f"[warn] player id map unavailable ({exc})")
        scout_ids = {}

    meta = (summary or {}).get("meta", {})
    return {
        "meta": {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "season": meta.get("season") or (matchup or {}).get("season"),
                 "week": meta.get("week") or (matchup or {}).get("week"),
                 "team": meta.get("team") or team_name, "league": meta.get("league"),
                 "pool_rows": len(pool_rows), "whatif_players": len(whatif)},
        "config": get("/api/config"),
        "summary": summary,
        "matchup": matchup,
        "roster": roster,
        "runs": get("/api/runs") or [],
        "news": get("/api/news"),
        "waivers": {"pool": {**(pool or {}), "players": pool_rows[: ecfg.get("pool_rows", 150)]}, "whatif": whatif},
        "players": players,
        "scout_ids": scout_ids,
        "errors": errors,
    }


def run(username: str, team_name: str, league_id: str | None, week: int | None, cfg: dict,
        out: str | Path, reports_dir: str | Path = "reports") -> Path:
    data = build(username, team_name, league_id, week, cfg, reports_dir)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":"), default=str))
    return out
