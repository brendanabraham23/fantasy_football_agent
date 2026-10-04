"""Waiver-wire recommendations: who improves the team, who to drop, what to bid."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .ranker import PlayerEval, lineup_total


@dataclass
class WaiverRec:
    player: PlayerEval
    weekly_gain: float      # change in this week's optimal lineup total
    ros_gain: float         # longer-term value vs. the suggested drop
    drop: PlayerEval | None
    bid: str
    score: float


def free_agent_pool(ctx, rostered: set[str], positions: set[str]) -> list[str]:
    per_pos = ctx.cfg["waivers"]["pool_per_position"]
    by_pos: dict[str, list[tuple[float, str]]] = {}
    for pid, p in ctx.players.items():
        pos = p.get("position")
        if pos not in positions or pid in rostered or not p.get("team") or p.get("active") is False:
            continue
        proj = (ctx.projections.get(pid) or {})
        pts = proj.get("pts_ppr") or proj.get("pts_half_ppr") or proj.get("pts_std") or 0
        if pts > 0 or pid in ctx.trending:
            by_pos.setdefault(pos, []).append((pts, pid))
    pool = {pid for rows in by_pos.values() for _, pid in sorted(rows, reverse=True)[:per_pos]}
    pool |= {pid for pid in ctx.trending if pid in ctx.players and pid not in rostered
             and ctx.players[pid].get("position") in positions}
    return sorted(pool)


def evaluate_add(ctx, my_evals: list[PlayerEval], starters_ids: set[str], cand: PlayerEval,
                 base_total: float | None = None) -> WaiverRec:
    """What adding `cand` is worth: lineup gain this week, value vs. the suggested drop, and a bid."""
    wcfg = ctx.cfg["waivers"]
    slots = ctx.league["roster_positions"]
    if base_total is None:
        base_total = lineup_total(my_evals, slots)
    bench = [e for e in my_evals if e.player_id not in starters_ids]

    settings = ctx.league.get("settings", {})
    rs = ctx.my_roster.get("settings", {}) or {}
    faab = settings.get("waiver_type") == 2
    budget_left = (settings.get("waiver_budget") or 0) - (rs.get("waiver_budget_used") or 0)

    weekly = round(lineup_total(my_evals + [cand], slots) - base_total, 2)
    # K/DEF are streamed like-for-like; everyone else replaces your weakest bench piece
    drop_pool = [e for e in bench if e.position == cand.position] if cand.position in ("K", "DEF") else \
                [e for e in bench if e.position not in ("K", "DEF")]
    drop = min(drop_pool, key=lambda e: e.ros_value, default=None)
    ros = round(cand.ros_value - (drop.ros_value if drop else 0), 2)
    trend = math.log1p(cand.trending_adds / 1000) if cand.trending_adds else 0
    score = round(max(weekly, 0) + 0.5 * max(ros, 0) + trend, 2)
    if faab and budget_left > 0:
        pct = min(wcfg["max_bid_pct"], 0.02 * max(weekly, 0) + 0.03 * max(ros, 0) + 0.04 * trend)
        bid = f"${max(1, round(budget_left * pct))} of ${budget_left}"
    elif faab:
        bid = "$0 (budget spent)"
    else:
        bid = f"claim (waiver #{rs.get('waiver_position', '?')})"
    return WaiverRec(cand, weekly, ros, drop, bid, score)


def recommend(ctx, evaluator, my_evals: list[PlayerEval], starters_ids: set[str],
              candidates: list[PlayerEval]) -> list[WaiverRec]:
    wcfg = ctx.cfg["waivers"]
    base_total = lineup_total(my_evals, ctx.league["roster_positions"])
    recs = [evaluate_add(ctx, my_evals, starters_ids, cand, base_total) for cand in candidates]
    recs = [r for r in recs if r.weekly_gain >= wcfg["min_weekly_gain"] or r.ros_gain >= wcfg["min_ros_gain"]]
    recs.sort(key=lambda r: r.score, reverse=True)
    return recs[: wcfg["show"]]
