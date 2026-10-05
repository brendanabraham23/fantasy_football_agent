"""Waiver-wire recommendations: who improves the team, who to drop, what to bid."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import sleeper
from .ranker import SLOT_ELIGIBILITY, PlayerEval, lineup_total


@dataclass
class WaiverRec:
    player: PlayerEval
    weekly_gain: float      # change in this week's optimal lineup total
    ros_gain: float         # longer-term value vs. the suggested drop
    drop: PlayerEval | None
    bid: str
    score: float
    bid_detail: dict = field(default_factory=dict)


@dataclass
class Market:
    """League-wide FAAB context: who else could use a player and what they can afford."""
    faab: bool
    budget_left: int
    weeks_left: int                      # regular-season weeks remaining, including this one
    budgets: dict                        # other roster_id -> FAAB left
    bars: dict                           # other roster_id -> {position: projection of their worst starter}


def build_market(ctx) -> Market:
    settings = ctx.league.get("settings") or {}
    budget = settings.get("waiver_budget") or 0
    left = lambda r: budget - ((r.get("settings") or {}).get("waiver_budget_used") or 0)
    slots = [s for s in ctx.league["roster_positions"] if s in SLOT_ELIGIBILITY]
    # starters per position: dedicated slots plus one for each flex the position can fill
    need = {pos: sum(pos in SLOT_ELIGIBILITY[s] for s in slots) for pos in sleeper.FANTASY_POSITIONS}
    end = settings.get("playoff_week_start") or ctx.cfg["waivers"].get("season_weeks", 17) + 1
    mine = ctx.my_roster.get("roster_id")
    budgets, bars = {}, {}
    for r in ctx.rosters:
        rid = r.get("roster_id")
        if rid == mine or r is ctx.my_roster:
            continue
        by_pos: dict[str, list[float]] = {}
        for pid in r.get("players") or []:
            pos = (ctx.players.get(pid) or {}).get("position")
            if pos in need:
                pts = sleeper.fantasy_points(ctx.projections.get(pid), ctx.scoring, ctx.rec_value, pos) or 0.0
                by_pos.setdefault(pos, []).append(pts)
        bars[rid] = {pos: (sorted(by_pos.get(pos, []), reverse=True) + [0.0] * k)[k - 1]
                     for pos, k in need.items() if k}
        budgets[rid] = left(r)
    return Market(settings.get("waiver_type") == 2, left(ctx.my_roster), max(1, end - ctx.week), budgets, bars)


def suggest_bid(wcfg: dict, market: Market, cand: PlayerEval, weekly: float, ros: float) -> tuple[int, dict]:
    """FAAB bid = budget x value share x demand, capped by what the richest interested team can pay.

    value  = this week's lineup gain + weekly gain vs. the drop over the next `bid_horizon_weeks`
    share  = max_bid_pct x value / (value + bid_half_value)        (rises smoothly, never saturates)
    demand = base + competition_weight x (share of other teams that would start him and have FAAB)
                  + trend_weight x log(1 + Sleeper adds/1000)
    """
    horizon = max(0, min(wcfg["bid_horizon_weeks"], market.weeks_left - 1))
    value = max(weekly, 0) + max(ros, 0) * horizon
    share = wcfg["max_bid_pct"] * value / (value + wcfg["bid_half_value"]) if value > 0 else 0.0
    rivals = [rid for rid, bar in market.bars.items()
              if (cand.proj or 0) > bar.get(cand.position, 0) and (market.budgets[rid] > 0 or not market.faab)]
    comp = len(rivals) / len(market.bars) if market.bars else 0.0
    trend = math.log1p(cand.trending_adds / 1000) if cand.trending_adds else 0.0
    demand = wcfg["bid_demand_base"] + wcfg["bid_competition_weight"] * comp + wcfg["bid_trend_weight"] * trend
    pct = min(wcfg["bid_cap_pct"], share * demand)
    bid = round(market.budget_left * pct)
    ceiling = max((market.budgets[r] for r in rivals), default=None)
    if ceiling is not None:
        bid = min(bid, ceiling + 1)
    bid = max(1, min(bid, market.budget_left)) if market.budget_left > 0 else 0
    detail = {"value": round(value, 1), "horizon": horizon, "rivals": len(rivals), "teams": len(market.bars),
              "rival_max_budget": ceiling, "trending_adds": cand.trending_adds, "pct": round(pct, 3)}
    return bid, detail


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
                 base_total: float | None = None, market: Market | None = None) -> WaiverRec:
    """What adding `cand` is worth: lineup gain this week, value vs. the suggested drop, and a bid."""
    wcfg = ctx.cfg["waivers"]
    slots = ctx.league["roster_positions"]
    if base_total is None:
        base_total = lineup_total(my_evals, slots)
    market = market or build_market(ctx)
    bench = [e for e in my_evals if e.player_id not in starters_ids]
    rs = ctx.my_roster.get("settings", {}) or {}

    weekly = round(lineup_total(my_evals + [cand], slots) - base_total, 2)
    # K/DEF are streamed like-for-like; everyone else replaces your weakest bench piece
    drop_pool = [e for e in bench if e.position == cand.position] if cand.position in ("K", "DEF") else \
                [e for e in bench if e.position not in ("K", "DEF")]
    drop = min(drop_pool, key=lambda e: e.ros_value, default=None)
    ros = round(cand.ros_value - (drop.ros_value if drop else 0), 2)
    trend = math.log1p(cand.trending_adds / 1000) if cand.trending_adds else 0
    score = round(max(weekly, 0) + 0.5 * max(ros, 0) + trend, 2)
    detail = {}
    if market.faab and market.budget_left > 0:
        amount, detail = suggest_bid(wcfg, market, cand, weekly, ros)
        bid = f"${amount} of ${market.budget_left}"
    elif market.faab:
        bid = "$0 (budget spent)"
    else:
        bid = f"claim (waiver #{rs.get('waiver_position', '?')})"
    return WaiverRec(cand, weekly, ros, drop, bid, score, detail)


def recommend(ctx, evaluator, my_evals: list[PlayerEval], starters_ids: set[str],
              candidates: list[PlayerEval]) -> list[WaiverRec]:
    wcfg = ctx.cfg["waivers"]
    base_total = lineup_total(my_evals, ctx.league["roster_positions"])
    market = build_market(ctx)
    recs = [evaluate_add(ctx, my_evals, starters_ids, cand, base_total, market) for cand in candidates]
    recs = [r for r in recs if r.weekly_gain >= wcfg["min_weekly_gain"] or r.ros_gain >= wcfg["min_ros_gain"]]
    recs.sort(key=lambda r: r.score, reverse=True)
    return recs[: wcfg["show"]]
