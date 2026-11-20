"""Usage-based recent form: expected points from targets and carries instead of the points actually scored.

Volume is steadier week to week than fantasy points, so it predicts the next game better (see
docs/usage-signal.md for the backtest). Points per target and per carry are fit per position on the
recent weeks' stat lines, in the league's own scoring.
"""
from __future__ import annotations

import numpy as np

from . import sleeper

TARGETS, CARRIES = "rec_tgt", "rush_att"


def _played(line: dict | None) -> bool:
    return bool(line) and (line.get("gp", 1) or 0) > 0


def _opps(line: dict) -> tuple[float, float]:
    return float(line.get(TARGETS) or 0), float(line.get(CARRIES) or 0)


def coefficients(recent: dict[int, dict], players: dict, scoring: dict, rec_value: float,
                 cfg: dict) -> dict[str, tuple[float, float]]:
    """position -> (points per target, points per carry), least squares over every player's recent games.

    Empty for a position with too few games or no recorded targets/carries, which switches the signal off there."""
    rows: dict[str, list] = {pos: [] for pos in cfg.get("positions", [])}
    for week in recent.values():
        for pid, line in week.items():
            pos = (players.get(pid) or {}).get("position")
            if pos in rows and _played(line) and sum(_opps(line)) > 0 \
                    and (pts := sleeper.fantasy_points(line, scoring, rec_value, pos)) is not None:
                rows[pos].append((*_opps(line), pts))
    out = {}
    for pos, r in rows.items():
        if len(r) < cfg.get("min_games", 40):
            continue
        a = np.array(r, dtype=float)
        coef, *_ = np.linalg.lstsq(a[:, :2], a[:, 2], rcond=None)
        if a[:, 0].sum() > 0 or a[:, 1].sum() > 0:
            out[pos] = (round(max(float(coef[0]), 0.0), 3), round(max(float(coef[1]), 0.0), 3))
    return out


def expected(lines: list[dict], coef: tuple[float, float]) -> tuple[float, float, float]:
    """(expected points, targets, carries) per game over the games in `lines`."""
    tgt = sum(_opps(l)[0] for l in lines) / len(lines)
    car = sum(_opps(l)[1] for l in lines) / len(lines)
    return round(coef[0] * tgt + coef[1] * car, 2), round(tgt, 1), round(car, 1)
