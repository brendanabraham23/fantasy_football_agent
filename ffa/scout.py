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
NICKNAMES = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers", "CHI": "Bears",
    "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos", "DET": "Lions", "GB": "Packers",
    "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars", "KC": "Chiefs", "LV": "Raiders", "LAC": "Chargers",
    "LA": "Rams", "MIA": "Dolphins", "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants",
    "NYJ": "Jets", "PHI": "Eagles", "PIT": "Steelers", "SF": "49ers", "SEA": "Seahawks", "TB": "Buccaneers",
    "TEN": "Titans", "WAS": "Commanders",
}
CITIES = {
    "ARI": "Arizona", "ATL": "Atlanta", "BAL": "Baltimore", "BUF": "Buffalo", "CAR": "Carolina", "CHI": "Chicago",
    "CIN": "Cincinnati", "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit", "GB": "Green Bay",
    "HOU": "Houston", "IND": "Indianapolis", "JAX": "Jacksonville", "KC": "Kansas City", "LV": "Las Vegas",
    "LAC": "Los Angeles", "LA": "Los Angeles", "MIA": "Miami", "MIN": "Minnesota", "NE": "New England",
    "NO": "New Orleans", "NYG": "New York", "NYJ": "New York", "PHI": "Philadelphia", "PIT": "Pittsburgh",
    "SF": "San Francisco", "SEA": "Seattle", "TB": "Tampa Bay", "TEN": "Tennessee", "WAS": "Washington",
}
ALIASES = {"SF": "niners", "NE": "pats", "TB": "bucs", "JAX": "jags", "MIA": "phins", "PHI": "birds", "GB": "pack",
           "LAC": "bolts", "WAS": "commies", "LA": "la rams", "LV": "lv"}
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


def dst_points(row: pd.Series, allowed: float | None, sc: dict) -> float:
    g = lambda c: float(row.get(c) or 0) if pd.notna(row.get(c)) else 0.0
    pts = (sc["sack"] * g("def_sacks") + sc["int"] * g("def_interceptions") + sc["fum_rec"] * g("fumble_recovery_opp")
           + sc["td"] * (g("def_tds") + g("special_teams_tds")) + sc["safety"] * g("def_safeties")
           + sc["blk_kick"] * (g("def_punt_blocks") + g("def_fg_blocks")))
    if allowed is not None:
        pts += next(v for cap, v in sc["points_allowed"] if allowed <= cap)
    return round(pts, 2)


def dst_players(season: int, cfg: dict, sched: pd.DataFrame, games: dict, implied_avg: float,
                next_week: int | None, model: dict, snap: dict | None) -> list[dict]:
    """Team defenses scored from nflverse team stats + final scores, projected like skill players.

    Matchup = how many D/ST points the opposing offense has given up per game (vs. league average),
    and the opponent's Vegas implied total (fewer expected points -> better for the defense).
    """
    try:
        ts = nflverse.team_stats(season)
    except Exception as exc:
        print(f"[warn] team stats unavailable ({exc}); D/STs skipped")
        return []
    sc, rw, n_recent = cfg["scout"]["dst_scoring"], cfg["scout"]["recent_weight"], cfg["recent_weeks"]
    ts = ts[ts["season_type"] == "REG"]
    reg = sched[(sched["game_type"] == "REG") & sched["home_score"].notna()] if "home_score" in sched else sched.iloc[0:0]
    allowed, home = {}, {}
    for _, g in reg.iterrows():
        allowed[(g["home_team"], int(g["week"]))] = float(g["away_score"])
        allowed[(g["away_team"], int(g["week"]))] = float(g["home_score"])
        home[(g["home_team"], int(g["week"]))] = True
        home[(g["away_team"], int(g["week"]))] = False
    rows = []
    for _, r in ts.iterrows():
        key = (r["team"], int(r["week"]))
        rows.append({"team": r["team"], "opp": r["opponent_team"], "w": int(r["week"]), "pa": allowed.get(key),
                     "pts": dst_points(r, allowed.get(key), sc), "home": home.get(key, True),
                     "sk": r.get("def_sacks"), "int": r.get("def_interceptions"), "fr": r.get("fumble_recovery_opp"),
                     "td": (r.get("def_tds") or 0) + (r.get("special_teams_tds") or 0)})
    df = pd.DataFrame(rows)
    if df.empty:
        return []
    # offense-side view: D/ST points each offense has given up per game, relative to league average
    offense = df[df["w"] < (next_week or 99)].groupby("opp")["pts"].mean()
    ratio = (offense / offense.mean()).to_dict() if len(offense) else {}
    order = sorted(ratio, key=ratio.get)                 # rank 1 = offense that gives up the fewest D/ST points
    lo, hi = cfg["matchup_clip"]
    out = []
    for team, g in df.sort_values("w").groupby("team"):
        weeks = [{"w": r.w, "opp": ("vs " if r.home else "@ ") + r.opp, "pts": r.pts,
                  **{k: (int(getattr(r, k)) if pd.notna(getattr(r, k)) else 0) for k in ("sk", "int", "fr", "td")},
                  **({"pa": int(r.pa)} if r.pa is not None and pd.notna(r.pa) else {})} for r in g.itertuples()]
        pts = [w["pts"] for w in weeks]
        season_avg, recent_avg = float(np.mean(pts)), float(np.mean(pts[-n_recent:]))
        p = {"id": f"DEF-{team}", "name": f"{NICKNAMES.get(team, team)} D/ST", "pos": "DEF", "team": team, "weeks": weeks,
             "aka": " ".join(filter(None, [CITIES.get(team), NICKNAMES.get(team), ALIASES.get(team), "defense dst d/st"])),
             "games": len(pts), "total": _r(sum(pts)), "avg": _r(season_avg), "recent": _r(recent_avg)}
        if next_week:
            game = games.get(team)
            proj = {"week": next_week}
            if not game:
                proj.update(opp="BYE", value=0.0, bye=True)
            else:
                base = rw * recent_avg + (1 - rw) * season_avg
                opp = game["opp"]
                m = 1 + cfg["matchup_weight"] * (ratio.get(opp, 1.0) - 1)
                if game.get("opp_implied") is not None:
                    m *= 1 - cfg["vegas_weight"] * (game["opp_implied"] / implied_avg - 1)
                m = max(lo, min(hi, m))
                proj.update(opp=("vs " if game["is_home"] else "@ ") + opp, kickoff=f"{game['gameday']} {game.get('gametime') or ''}".strip(),
                            implied=_r(game.get("opp_implied"), 1), base=_r(base), mult_matchup=_r(m, 3), mult_injury=1.0,
                            value=_r(base * m))
                if opp in ratio:
                    rank, n = order.index(opp) + 1, len(order)
                    pct = rank / n
                    proj.update(def_rank=rank, def_teams=n, def_ratio=_r(ratio[opp], 2),
                                grade="A" if pct > 0.8 else "B" if pct > 0.6 else "C" if pct > 0.4 else "D" if pct > 0.2 else "F")
            p["proj"] = proj
        mv = model.get(("def", team))
        if mv:
            p["model"] = {"week": snap["meta"]["week"], "adj": mv.get("adj"), "sleeper_proj": mv.get("proj"),
                          "sentiment": None, "n_articles": 0, "mult_sentiment": None, "injury_status": None,
                          "weather": mv.get("weather"), "notes": mv.get("notes") or [], "articles": []}
            if mv.get("group") in ("roster", "reserve"):
                p["mine"] = True
        out.append(p)
    return out


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
        model |= {("def", d.get("team")): d for d in snap["players"].values() if d.get("position") == "DEF"}

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

    players += dst_players(season, cfg, sched, games, implied_avg, next_week, model, snap)

    for pos in POSITIONS + ["DEF"]:   # positional rank by next-week projection
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
