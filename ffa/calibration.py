"""How well do base projections predict what players actually score?

    python -m ffa calibrate [--username USER] [--seasons 2025,2026] [--weeks 1-4]

For every player-week with a Sleeper projection, compares several predictors with the actual points
(league scoring when a username is given, else Sleeper's PPR totals at `--ppr`):

    sleeper  Sleeper's weekly projection (what `base` starts from)
    recent   average of the player's last `recent_weeks` games before that week
    season_avg  season-to-date average before that week
    base     the model's blend: weights.projection x sleeper + weights.recent_form x recent

Metrics follow docs/eval-loop-design.md §4.5: bias (predicted - actual), MAE, RMSE, Spearman rank
correlation within position-week, actual vs. predicted by projection bucket, plus the linear recalibration
actual ~ a + b x predicted (b < 1 means projections are too spread out: high ones overshoot, low ones
undershoot) and the projection/recent-form blend weight that would have minimized MAE.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import sleeper
from .ranker import player_name

PREDICTORS = ["sleeper", "recent", "season_avg", "base"]


def collect(season: int, weeks: list[int], scoring: dict, rec_value: float, players: dict, cfg: dict) -> pd.DataFrame:
    """One row per projected player-week: predictors, actual points and whether he played."""
    n_recent, w = cfg["recent_weeks"], cfg["weights"]
    stats = {}

    def week_stats(wk):
        if wk not in stats:
            stats[wk] = sleeper.stats(season, wk) if wk >= 1 else {}
        return stats[wk]

    def pts(line, pos):
        return sleeper.fantasy_points(line, scoring, rec_value, pos)

    history: dict[str, list[tuple[int, float]]] = {}   # player -> [(week, points)] for games played
    for wk in range(1, min(weeks)):
        for pid, line in week_stats(wk).items():
            pos = (players.get(pid) or {}).get("position")
            if pos in sleeper.FANTASY_POSITIONS and (line.get("gp", 1) or 0) > 0 and (v := pts(line, pos)) is not None:
                history.setdefault(pid, []).append((wk, v))

    rows = []
    for wk in sorted(weeks):
        proj = sleeper.projections(season, wk)
        actual_week = week_stats(wk)
        for pid, line in proj.items():
            p = players.get(pid) or {}
            pos = p.get("position")
            if pos not in sleeper.FANTASY_POSITIONS:
                continue
            sp = pts(line, pos)
            if not sp or sp <= 0:
                continue
            past = [v for _, v in history.get(pid, [])]
            recent = float(np.mean(past[-n_recent:])) if past else None
            season_avg = float(np.mean(past)) if past else None
            base = w["projection"] * sp + w["recent_form"] * recent if recent is not None else sp
            got = actual_week.get(pid)
            played = bool(got) and (got.get("gp", 1) or 0) > 0
            rows.append({"season": season, "week": wk, "player_id": pid, "name": player_name(p, pid), "position": pos,
                         "sleeper": round(sp, 2), "recent": recent, "season_avg": season_avg, "base": round(base, 2),
                         "actual": (pts(got, pos) or 0.0) if played else 0.0, "played": played})
        for pid, line in actual_week.items():   # this week becomes history for the next
            pos = (players.get(pid) or {}).get("position")
            if pos in sleeper.FANTASY_POSITIONS and (line.get("gp", 1) or 0) > 0 and (v := pts(line, pos)) is not None:
                history.setdefault(pid, []).append((wk, v))
    return pd.DataFrame(rows)


def metrics(df: pd.DataFrame, col: str) -> dict:
    d = df.dropna(subset=[col])
    if d.empty:
        return {"n": 0}
    err = d[col] - d["actual"]
    groups = [g for _, g in d.groupby(["season", "week", "position"]) if len(g) >= 5]
    rho = np.nanmean([g[col].corr(g["actual"], method="spearman") for g in groups]) if groups else np.nan
    b, a = np.polyfit(d[col], d["actual"], 1) if d[col].nunique() > 1 else (np.nan, np.nan)
    return {"n": len(d), "bias": round(err.mean(), 2), "mae": round(err.abs().mean(), 2),
            "rmse": round(float(np.sqrt((err ** 2).mean())), 2), "spearman": round(float(rho), 3),
            "slope": round(float(b), 3), "intercept": round(float(a), 2)}


def buckets(df: pd.DataFrame, col: str, n: int) -> pd.DataFrame:
    d = df.dropna(subset=[col]).copy()
    d["bucket"] = pd.qcut(d[col].rank(method="first"), q=min(n, len(d)), labels=False)
    out = d.groupby("bucket").agg(n=(col, "size"), pred_lo=(col, "min"), pred_hi=(col, "max"),
                                  predicted=(col, "mean"), actual=("actual", "mean"),
                                  dnp_rate=("played", lambda s: 1 - s.mean()))
    out["ratio"] = out["actual"] / out["predicted"]
    return out.round(2).reset_index(drop=True)


def best_blend(df: pd.DataFrame) -> tuple[float, float]:
    """Projection weight in w x sleeper + (1 - w) x recent that minimizes MAE, and that MAE."""
    d = df.dropna(subset=["recent"])
    if d.empty:
        return float("nan"), float("nan")
    grid = [(w, (w * d["sleeper"] + (1 - w) * d["recent"] - d["actual"]).abs().mean()) for w in np.linspace(0, 1, 21)]
    w, mae = min(grid, key=lambda t: t[1])
    return round(float(w), 2), round(float(mae), 2)


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join("-" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v) for v in r) + " |"
            for r in rows]
    return "\n".join(out)


def _metric_rows(df: pd.DataFrame, cols: list[str]) -> list[list]:
    keys = ["n", "bias", "mae", "rmse", "spearman", "slope", "intercept"]
    return [[c] + [metrics(df, c).get(k) for k in keys] for c in cols]


METRIC_HEAD = ["Predictor", "n", "Bias (pred - actual)", "MAE", "RMSE", "Spearman (pos-week)", "Slope", "Intercept"]


def render(df: pd.DataFrame, cfg: dict, label: str) -> str:
    ccfg = cfg.get("calibration", {})
    played = df[df["played"]]
    pool = played[played["sleeper"] >= ccfg.get("min_proj", 3)]
    lines = [f"# Projection calibration: {label}", "",
             f"{len(df):,} projected player-weeks; {len(played):,} played. Metrics below use players who played and were "
             f"projected for at least {ccfg.get('min_proj', 3)} points ({len(pool):,} rows), unless noted.", ""]

    lines += ["## Predictors compared", "", _table(METRIC_HEAD, _metric_rows(pool, PREDICTORS)), ""]
    s = metrics(pool, "sleeper")
    notes = []
    if s.get("n"):
        if s["slope"] < 0.95:
            notes.append(f"Sleeper projections are **too spread out** (slope {s['slope']}): for each extra projected point, "
                         f"players scored about {s['slope']:.2f}. Shrinking projections toward the position mean would help.")
        elif s["slope"] > 1.05:
            notes.append(f"Sleeper projections are **too compressed** (slope {s['slope']}): differences between players are "
                         f"bigger than projected.")
        if abs(s["bias"]) >= 0.5:
            notes.append(f"Sleeper projections run **{'high' if s['bias'] > 0 else 'low'}** by {abs(s['bias'])} pts per player-week.")
        best = min(PREDICTORS, key=lambda c: metrics(pool, c).get("mae", np.inf))
        notes.append(f"Lowest MAE: **{best}**.")
    w, mae = best_blend(pool)
    notes.append(f"Best projection weight in the base blend: **{w}** (MAE {mae}); config uses "
                 f"{cfg['weights']['projection']}.")
    lines += [f"- {n}" for n in notes] + [""]

    lines += ["## By position (Sleeper projection vs. model base)", ""]
    rows = []
    for pos in sleeper.FANTASY_POSITIONS:
        g = pool[pool["position"] == pos]
        if g.empty:
            continue
        for c in ("sleeper", "base"):
            m = metrics(g, c)
            rows.append([pos, c, m.get("n"), m.get("bias"), m.get("mae"), m.get("spearman"), m.get("slope")])
    lines += [_table(["Pos", "Predictor", "n", "Bias", "MAE", "Spearman", "Slope"], rows), ""]

    nb = ccfg.get("buckets", 5)
    lines += [f"## Calibration buckets (Sleeper projection, {nb} equal-size buckets)", "",
              "Includes players who didn't play (scored 0) so the DNP rate shows how often a projection was wasted.", ""]
    for pos in sleeper.FANTASY_POSITIONS:
        g = df[(df["position"] == pos) & (df["sleeper"] >= ccfg.get("min_proj", 3))]
        if len(g) < nb * 5:
            continue
        b = buckets(g, "sleeper", nb)
        lines += [f"### {pos}", "", _table(["Projected range", "n", "Avg projected", "Avg actual", "Actual / projected", "DNP rate"],
                  [[f"{r.pred_lo:.1f}-{r.pred_hi:.1f}", int(r.n), r.predicted, r.actual, r.ratio, f"{r.dnp_rate:.0%}"]
                   for r in b.itertuples()]), ""]
    return "\n".join(lines)


def run(seasons: list[int], weeks: list[int] | None, cfg: dict, out_dir: str | Path = "reports",
        username: str | None = None, team_name: str = "", league_id: str | None = None,
        ppr: float = 1.0) -> tuple[Path, Path]:
    players = sleeper.all_players()
    scoring, rec_value = {}, ppr
    if username:
        lg = sleeper.find_my_team(username, team_name, seasons[-1], league_id)[0]
        scoring = lg.get("scoring_settings") or {}
        rec_value = float(scoring.get("rec", 0))
    state = sleeper.nfl_state()
    frames = []
    for season in seasons:
        wks = weeks or (list(range(1, int(state["week"]))) if season == int(state["season"]) else list(range(1, 19)))
        if not wks:
            continue
        print(f"  - {season}: weeks {wks[0]}-{wks[-1]}", flush=True)
        frames.append(collect(season, wks, scoring, rec_value, players, cfg))
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        raise ValueError("No projected player-weeks found for those seasons/weeks")
    label = ", ".join(f"{s} wk {df[df.season == s].week.min()}-{df[df.season == s].week.max()}" for s in sorted(df.season.unique()))
    out = Path(out_dir) / "calibration"
    out.mkdir(parents=True, exist_ok=True)
    stem = "calibration_" + "_".join(str(s) for s in seasons)
    md, csv = out / f"{stem}.md", out / f"{stem}_rows.csv"
    md.write_text(render(df, cfg, label + (" (league scoring)" if username else f" (PPR {ppr})")))
    df.to_csv(csv, index=False)
    return md, csv
