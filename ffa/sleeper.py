"""Sleeper API client (https://docs.sleeper.com).

The v1 endpoints are official. Weekly projections/stats come from api.sleeper.com,
which is what the Sleeper app itself uses but is undocumented and could change.
"""
from __future__ import annotations

from . import http

API = "https://api.sleeper.app/v1"
API2 = "https://api.sleeper.com"

FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]

# Sleeper -> nflverse team abbreviations
TEAM_FIX = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "LVR": "LV", "OAK": "LV", "SD": "LAC", "STL": "LA"}


def norm_team(team: str | None) -> str | None:
    return TEAM_FIX.get(team, team) if team else team


# ---- official endpoints -------------------------------------------------------

def nfl_state() -> dict:
    return http.get_json(f"{API}/state/nfl", ttl=1800)


def get_user(username: str) -> dict:
    user = http.get_json(f"{API}/user/{username}")
    if not user:
        raise ValueError(f"Sleeper user '{username}' not found")
    return user


def user_leagues(user_id: str, season: int) -> list[dict]:
    return http.get_json(f"{API}/user/{user_id}/leagues/nfl/{season}") or []


def league(league_id: str) -> dict:
    return http.get_json(f"{API}/league/{league_id}")


def league_users(league_id: str) -> list[dict]:
    return http.get_json(f"{API}/league/{league_id}/users") or []


def league_rosters(league_id: str) -> list[dict]:
    return http.get_json(f"{API}/league/{league_id}/rosters") or []


def transactions(league_id: str, week: int) -> list[dict]:
    """All transactions (waiver, free_agent, trade; complete and failed) for one week ("leg")."""
    return http.get_json(f"{API}/league/{league_id}/transactions/{week}", ttl=900) or []


def matchups(league_id: str, week: int, ttl: int = 900) -> list[dict]:
    """Per-roster `matchup_id`, `players`, `starters`, `players_points` and `points` (league scoring) for one week.

    Points update live during games, so live views pass a short ttl.
    """
    return http.get_json(f"{API}/league/{league_id}/matchups/{week}", ttl=ttl) or []


def all_players() -> dict:
    """~5MB dump of every NFL player. Sleeper asks that this be fetched at most daily."""
    return http.get_json(f"{API}/players/nfl", ttl=86400, timeout=60)


def trending_adds(lookback_hours: int = 48, limit: int = 50) -> dict[str, int]:
    rows = http.get_json(
        f"{API}/players/nfl/trending/add",
        params={"lookback_hours": lookback_hours, "limit": limit},
        ttl=3600,
    ) or []
    return {str(r["player_id"]): int(r.get("count", 0)) for r in rows}


# ---- weekly projections / stats (undocumented) --------------------------------

def _weekly(kind: str, season: int, week: int, positions=FANTASY_POSITIONS) -> dict[str, dict]:
    params = [("season_type", "regular")] + [("position[]", p) for p in positions]
    data = http.get_json(f"{API2}/{kind}/nfl/{season}/{week}", params=params, ttl=3600)
    items = [{"player_id": k, "stats": v} for k, v in data.items()] if isinstance(data, dict) else (data or [])
    return {str(it.get("player_id")): (it.get("stats") or {}) for it in items if it.get("player_id")}


def projections(season: int, week: int) -> dict[str, dict]:
    return _weekly("projections", season, week)


def stats(season: int, week: int) -> dict[str, dict]:
    return _weekly("stats", season, week)


def fantasy_points(stat_line: dict | None, scoring: dict, rec_value: float, position: str) -> float | None:
    """Score a stat line with the league's own scoring settings.

    K and DEF use Sleeper's precomputed totals because their scoring relies on
    bucketed keys (points-allowed tiers, FG distance) that raw projections omit.
    """
    if not stat_line:
        return None
    fallback_key = {1.0: "pts_ppr", 0.5: "pts_half_ppr"}.get(rec_value, "pts_std")
    fallback = stat_line.get(fallback_key, stat_line.get("pts_ppr"))
    if position in ("K", "DEF"):
        return round(float(fallback or 0), 2)
    pts = sum(
        float(stat_line[k]) * float(v)
        for k, v in scoring.items()
        if isinstance(stat_line.get(k), (int, float))
    )
    if abs(pts) < 1e-9 and fallback is not None:
        pts = float(fallback)
    return round(pts, 2)


# ---- finding your team ---------------------------------------------------------

def team_label(user: dict) -> str:
    return ((user.get("metadata") or {}).get("team_name") or user.get("display_name") or "").strip()


def find_my_team(username: str, team_name: str, season: int, league_id: str | None = None):
    """Return (league, users, rosters, my_roster, my_user).

    Searches the user's leagues for a team called `team_name` (case-insensitive).
    Falls back to the user's own roster in the first league if no name matches.
    """
    me = get_user(username)
    leagues = [league(league_id)] if league_id else user_leagues(me["user_id"], season)
    if not leagues:
        raise ValueError(f"No {season} NFL leagues found for '{username}'")

    fallback = None
    for lg in leagues:
        users = league_users(lg["league_id"])
        rosters = league_rosters(lg["league_id"])
        by_owner = {r.get("owner_id"): r for r in rosters}
        for u in users:
            if team_name and team_label(u).lower() == team_name.lower() and u["user_id"] in by_owner:
                return lg, users, rosters, by_owner[u["user_id"]], u
        if fallback is None and me["user_id"] in by_owner:
            mine = next(u for u in users if u["user_id"] == me["user_id"])
            fallback = (lg, users, rosters, by_owner[me["user_id"]], mine)

    if fallback:
        print(f"[warn] No team named '{team_name}' found; using {username}'s roster in "
              f"'{fallback[0].get('name')}' ({team_label(fallback[4])}).")
        return fallback
    raise ValueError(f"Could not find team '{team_name}' or a roster owned by '{username}'")
