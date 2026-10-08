"""Data file for the Scout artifact: every fantasy-relevant player's season, usage, next matchup and projection.

    python -m ffa scout-data [--season 2026] [--out reports/scout/data.json]

Built from nflverse (weekly stats, schedule and Vegas lines, injury reports), which needs no Sleeper access.
The projection here is a transparent baseline, not the pipeline's model:

    base = recent_weight x avg(last `recent_weeks` games) + (1 - recent_weight) x season avg
    proj = base x injury x matchup        (matchup = defense vs. position and Vegas implied total, as in ranker)

When reports/latest_run.json is from the same season, each matched player also gets the pipeline's view
(`model`: Sleeper projection, adjusted points, news sentiment and articles) and your roster is flagged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import nflverse, snapshot

POSITIONS = ["QB", "RB", "WR", "TE", "K"]
STAT_KEYS = {  # nflverse column -> short key in the data file
    "attempts": "pa", "passing_yards": "py", "passing_tds": "ptd", "passing_interceptions": "int",
    "carries": "car", "rushing_yards": "ry", "rushing_tds": "rtd", "targets": "tgt", "receptions": "rec",
    "receiving_yards": "recy", "receiving_tds": "rectd", "fg_made": "fgm", "fg_att": "fga", "pat_made": "xpm",
}
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(name: str) -> str:
    return SUFFIX.sub("", re.sub(r"[^a-z ]", "", (name or "").lower())).strip()


def _r(v, nd=2):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), nd)


def _points(df: pd.DataFrame, ppr: float) -> pd.Series:
    col = lambda c: df[c].fillna(0) if c in df.columns else 0
    pts = col("fantasy_points") + ppr * col("receptions")
    is_k = df["position"] == "K"
    return pts.where(~is_k, nflverse.kicker_points(df[is_k]).reindex(df.index))


def _home_away(game_id: str, team: str, opp: str) -> str:
    parts = str(game_id).split("_")   # 2026_04_AWAY_HOME
    return ("@ " if len(parts) == 4 and parts[2] == team else "vs ") + str(opp)


def build(season: int, cfg: dict, snap: dict | None = None) -> dict:
    scfg = cfg["scout"]
    rw, n_recent = scfg["recent_weight"], cfg["recent_weeks"]
    ppr = (snap or {}).get("meta", {}).get("rec_value", scfg["ppr"])   # your league's PPR when a run exists
    stats = nflverse.weekly_stats(season)
    stats = stats[stats["season_type"] == "REG"].copy()
    stats["position"] = stats["position"].replace({"FB": "RB"})
    stats = stats[stats["position"].isin(POSITIONS)]
    stats["pts"] = _points(stats, ppr).round(2)
    sched = nflverse.schedule(season)
    reg = sched[sched["game_type"] == "REG"]
    unplayed = reg[reg["home_score"].isna()] if "home_score" in reg else reg
    next_week = int(unplayed["week"].min()) if not unplayed.empty else None
    through = int(stats["week"].max()) if not stats.empty else 0

    games, implied_avg = nflverse.matchups(sched, next_week) if next_week else ({}, 22.5)
    defense = nflverse.defense_vs_position(stats, next_week or through + 1, ppr, cfg.get("defense_lookback_weeks"))
    try:
        inj = nflverse.injuries(season)
        inj = inj[inj["week"] == next_week].drop_duplicates("gsis_id", keep="last").set_index("gsis_id")
    except Exception as exc:
        print(f"[warn] injury reports unavailable ({exc})")
        inj = pd.DataFrame()

    model = {}
    if snap and snap["meta"]["season"] == season:
        model = {(norm_name(d["name"]), d["position"]): d for d in snap["players"].values()}

    players = []
    lo, hi = cfg["matchup_clip"]
    for pid, g in stats.sort_values("week").groupby("player_id"):
        last = g.iloc[-1]
        pos, team = last["position"], last["team"]
        weeks = [{"w": int(r.week), "opp": _home_away(r.game_id, r.team, r.opponent_team), "pts": _r(r.pts),
                  **{k: int(r[c]) if float(r[c]).is_integer() else _r(r[c], 1)
                     for c, k in STAT_KEYS.items() if c in g.columns and pd.notna(r[c]) and r[c]},
                  **({"tshare": _r(r.target_share, 3)} if pd.notna(r.get("target_share")) and r.get("target_share") else {})}
                 for _, r in g.iterrows()]
        pts = [w["pts"] for w in weeks if w["pts"] is not None]
        season_avg = float(np.mean(pts)) if pts else None
        recent_avg = float(np.mean(pts[-n_recent:])) if pts else None
        p = {"id": pid, "name": last["player_display_name"], "pos": pos, "team": team, "weeks": weeks,
             "games": len(pts), "total": _r(sum(pts)), "avg": _r(season_avg), "recent": _r(recent_avg)}

        status = inj.loc[pid] if pid in inj.index else None
        if status is not None:
            p["injury"] = {"status": status.get("report_status") if pd.notna(status.get("report_status")) else None,
                           "practice": status.get("practice_status") if pd.notna(status.get("practice_status")) else None,
                           "detail": status.get("report_primary_injury") if pd.notna(status.get("report_primary_injury")) else None}

        if next_week:
            game = games.get(team)
            proj = {"week": next_week}
            if not game:
                proj.update(opp="BYE", value=0.0, bye=True)
            elif season_avg is not None:
                base = rw * recent_avg + (1 - rw) * season_avg
                m, d = 1.0, None
                row = defense[(defense["opponent_team"] == game["opp"]) & (defense["position"] == pos)] \
                    if not defense.empty else pd.DataFrame()
                if not row.empty:
                    d = row.iloc[0]
                    m *= 1 + cfg["matchup_weight"] * (float(d["ratio"]) - 1)
                if game.get("implied") is not None:
                    m *= 1 + cfg["vegas_weight"] * (game["implied"] / implied_avg - 1)
                m = max(lo, min(hi, m))
                inj_status = (p.get("injury") or {}).get("status")
                im = cfg["injury_multipliers"].get(inj_status or "", 1.0)
                proj.update(opp=("vs " if game["is_home"] else "@ ") + game["opp"], kickoff=f"{game['gameday']} {game.get('gametime') or ''}".strip(),
                            implied=_r(game.get("implied"), 1), base=_r(base), mult_matchup=_r(m, 3), mult_injury=im,
                            value=_r(base * m * im))
                if d is not None:
                    rank, n = int(d["rank"]), int(d["n_teams"])
                    pct = rank / n
                    proj.update(def_rank=rank, def_teams=n, def_ratio=_r(d["ratio"], 2),
                                grade="A" if pct > 0.8 else "B" if pct > 0.6 else "C" if pct > 0.4 else "D" if pct > 0.2 else "F")
            p["proj"] = proj

        mv = model.get((norm_name(p["name"]), pos))
        if mv:
            p["model"] = {"week": snap["meta"]["week"], "adj": mv.get("adj"), "sleeper_proj": mv.get("proj"),
                          "sentiment": mv.get("sentiment"), "n_articles": mv.get("n_articles"),
                          "mult_sentiment": mv.get("mult_sentiment"), "injury_status": mv.get("injury_status"),
                          "weather": mv.get("weather"), "notes": mv.get("notes") or [],
                          "articles": [{k: a.get(k) for k in ("title", "link", "score", "source", "published")}
                                       for a in (mv.get("articles") or [])[: scfg["max_articles"]]]}
            if mv.get("group") in ("roster", "reserve"):
                p["mine"] = True
        players.append(p)

    for pos in POSITIONS:   # positional rank by next-week projection
        ranked = sorted((p for p in players if p["pos"] == pos and (p.get("proj") or {}).get("value")),
                        key=lambda p: p["proj"]["value"], reverse=True)
        for i, p in enumerate(ranked, 1):
            p["proj"]["pos_rank"] = i
    players.sort(key=lambda p: ((p.get("proj") or {}).get("value") or 0, p["total"] or 0), reverse=True)

    teams = {}
    for team in sorted(set(reg["home_team"]) | set(reg["away_team"]) | {p["team"] for p in players}):
        played = set(reg[(reg["home_team"] == team) | (reg["away_team"] == team)]["week"])
        teams[team] = {"bye": sorted(set(reg["week"]) - played)}
        if next_week and team in games:
            gm = games[team]
            teams[team].update(next_opp=("vs " if gm["is_home"] else "@ ") + gm["opp"], implied=_r(gm.get("implied"), 1),
                               total=_r(gm.get("total"), 1), kickoff=f"{gm['gameday']} {gm.get('gametime') or ''}".strip())
    return {
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "season": season,
                 "through_week": through, "next_week": next_week, "ppr": ppr, "recent_weight": rw,
                 "recent_weeks": n_recent, "implied_avg": _r(implied_avg, 1),
                 "model_run": ({"generated_at": snap["meta"]["generated_at"], "week": snap["meta"]["week"],
                                "team": snap["meta"].get("team"), "news": snap["meta"].get("options", {}).get("news", True)}
                               if model else None),
                 "sources": ["nflverse weekly stats, schedule & Vegas lines, injury reports"]
                            + (["your latest pipeline run (Sleeper projections, model points, news)"] if model else [])},
        "teams": teams,
        "players": players,
    }


def run(season: int, cfg: dict, out: str | Path, reports_dir: str | Path = "reports") -> Path:
    data = build(season, cfg, snapshot.load(reports_dir))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":"), default=str))
    return out
