"""Exact best-lineup solver (max-weight assignment of players to starting slots).

Used for hindsight evaluation, where greedy filling can be wrong when flex slots
overlap (e.g. a WR/TE flex plus a WR/RB flex).
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment

from .ranker import NON_STARTING, SLOT_ELIGIBILITY

_INELIGIBLE = 1e9


def starting_slots(roster_positions: list[str]) -> list[str]:
    return [s for s in roster_positions if s not in NON_STARTING and s in SLOT_ELIGIBILITY]


def player_positions(players: dict) -> dict[str, set[str]]:
    """Sleeper player dump -> {player_id: eligible positions} (uses fantasy_positions when present)."""
    out = {}
    for pid, p in players.items():
        pos = p.get("fantasy_positions") or ([p["position"]] if p.get("position") else [])
        out[pid] = set(pos)
    return out


def best_lineup(points: dict[str, float], positions: dict[str, set[str]],
                roster_positions: list[str]) -> tuple[float, list[tuple[str, str | None]]]:
    """Return (total, [(slot, player_id or None)]) maximizing total points.

    A slot can be left empty (0 points) if nobody eligible scores above zero.
    """
    slots = starting_slots(roster_positions)
    pids = [p for p in points if positions.get(p)]
    if not slots:
        return 0.0, []
    # rows = real players + one "empty" dummy per slot; columns = slots
    cost = np.zeros((len(pids) + len(slots), len(slots)))
    for i, pid in enumerate(pids):
        for j, slot in enumerate(slots):
            cost[i, j] = -points[pid] if positions[pid] & SLOT_ELIGIBILITY[slot] else _INELIGIBLE
    rows, cols = linear_sum_assignment(cost)
    lineup: list[tuple[str, str | None]] = [(s, None) for s in slots]
    total = 0.0
    for r, c in zip(rows, cols):
        if r < len(pids) and cost[r, c] < _INELIGIBLE:
            lineup[c] = (slots[c], pids[r])
            total += points[pids[r]]
    return round(total, 2), lineup


def best_total(roster: set[str], points: dict[str, float], positions: dict[str, set[str]],
               roster_positions: list[str]) -> float:
    return best_lineup({p: points.get(p, 0.0) for p in roster}, positions, roster_positions)[0]
