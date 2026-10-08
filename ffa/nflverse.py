"""Schedule, Vegas lines and defense-vs-position from nflverse (free, updated nightly).

Replaces the `fantasyfootball` package, whose bundled data ends with the 2022 season.
"""
from __future__ import annotations

import io

import pandas as pd

from . import http

REL = "https://github.com/nflverse/nflverse-data/releases/download"


def _csv(url: str, ttl: int) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(http.get(url, ttl=ttl, timeout=60)), low_memory=False)


def schedule(season: int) -> pd.DataFrame:
    df = _csv(f"{REL}/schedules/games.csv", ttl=6 * 3600)
    return df[df["season"] == season].copy()


def weekly_stats(season: int) -> pd.DataFrame:
    return _csv(f"{REL}/stats_player/stats_player_week_{season}.csv", ttl=6 * 3600)


def injuries(season: int) -> pd.DataFrame:
    """Weekly injury reports (report_status: Questionable/Doubtful/Out; practice_status), keyed by gsis_id."""
    return _csv(f"{REL}/injuries/injuries_{season}.csv", ttl=3 * 3600)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name].fillna(0) if name in df.columns else pd.Series(0, index=df.index)


def kicker_points(df: pd.DataFrame) -> pd.Series:
    short = sum(_col(df, c) for c in ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39"))
    return (3 * short + 4 * _col(df, "fg_made_40_49")
            + 5 * (_col(df, "fg_made_50_59") + _col(df, "fg_made_60_"))
            + _col(df, "pat_made") - _col(df, "fg_missed"))


def defense_vs_position(stats: pd.DataFrame, before_week: int, rec_value: float,
                        lookback_weeks: int | None = None) -> pd.DataFrame:
    """Fantasy points each defense allows per game to each position.

    rank 1 = stingiest defense. ratio > 1 means the defense allows more than average.
    """
    cols = ["opponent_team", "position", "pts_allowed_pg", "ratio", "rank", "n_teams"]
    if stats is None or stats.empty:
        return pd.DataFrame(columns=cols)
    df = stats[(stats["season_type"] == "REG") & (stats["week"] < before_week)].copy()
    if lookback_weeks:
        df = df[df["week"] >= before_week - lookback_weeks]
    df["position"] = df["position"].replace({"FB": "RB"})
    df = df[df["position"].isin(["QB", "RB", "WR", "TE", "K"])]
    if df.empty:
        return pd.DataFrame(columns=cols)

    # nflverse `fantasy_points` is standard scoring; add the league's per-reception value.
    df["fp"] = _col(df, "fantasy_points") + rec_value * _col(df, "receptions")
    is_k = df["position"] == "K"
    df.loc[is_k, "fp"] = kicker_points(df[is_k])

    per_game = df.groupby(["opponent_team", "position", "week"])["fp"].sum().reset_index()
    agg = per_game.groupby(["opponent_team", "position"])["fp"].mean().reset_index(name="pts_allowed_pg")
    agg["ratio"] = agg["pts_allowed_pg"] / agg.groupby("position")["pts_allowed_pg"].transform("mean")
    agg["rank"] = agg.groupby("position")["pts_allowed_pg"].rank(method="min").astype(int)
    agg["n_teams"] = agg.groupby("position")["pts_allowed_pg"].transform("count").astype(int)
    return agg


def matchups(sched: pd.DataFrame, week: int) -> tuple[dict, float]:
    """Map team -> game info for `week`, plus the week's average implied team total.

    nflverse spread_line is positive when the home team is favored.
    """
    out, implied = {}, []
    wk = sched[(sched["week"] == week) & (sched["game_type"] == "REG")]
    for _, g in wk.iterrows():
        total, spread = g.get("total_line"), g.get("spread_line")
        home_it = away_it = None
        if pd.notna(total) and pd.notna(spread):
            home_it, away_it = (total + spread) / 2, (total - spread) / 2
            implied += [home_it, away_it]
        base = {
            "game_id": g["game_id"], "gameday": g["gameday"], "gametime": g.get("gametime"),
            "roof": g.get("roof"), "stadium": g.get("stadium"), "neutral": g.get("location") == "Neutral",
            "home_team": g["home_team"], "total": total,
        }
        out[g["home_team"]] = {**base, "opp": g["away_team"], "is_home": True,
                               "implied": home_it, "opp_implied": away_it}
        out[g["away_team"]] = {**base, "opp": g["home_team"], "is_home": False,
                               "implied": away_it, "opp_implied": home_it}
    avg = sum(implied) / len(implied) if implied else 22.5
    return out, avg
