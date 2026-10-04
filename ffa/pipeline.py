"""Orchestrates one weekly run: fetch -> evaluate -> lineup -> waivers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import nflverse, sleeper, waivers, weather
from .news import NewsScorer
from .ranker import Evaluator, PlayerEval, optimal_lineup

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def load_config(path: str | Path | None = None) -> dict:
    with open(path or DEFAULT_CONFIG) as f:
        return json.load(f)


@dataclass
class Context:
    cfg: dict
    season: int
    week: int
    league: dict
    my_roster: dict
    my_team_name: str
    scoring: dict
    rec_value: float
    players: dict
    projections: dict
    recent: dict
    defense: pd.DataFrame
    matchups: dict
    implied_avg: float
    trending: dict
    users: list = field(default_factory=list)
    rosters: list = field(default_factory=list)
    _wx: dict = field(default_factory=dict)

    @property
    def rostered(self) -> set[str]:
        return {pid for r in self.rosters for key in ("players", "reserve", "taxi") for pid in (r.get(key) or [])}

    @property
    def fantasy_positions(self) -> set[str]:
        slots = self.league["roster_positions"]
        positions = {s for s in slots if s in sleeper.FANTASY_POSITIONS}
        if any("FLEX" in s for s in slots):
            positions |= {"RB", "WR", "TE"}
        return positions

    def owners(self) -> dict[str, str]:
        """player_id -> team name of the roster holding them."""
        names = {u["user_id"]: sleeper.team_label(u) for u in self.users}
        return {pid: names.get(r.get("owner_id")) or f"Team {r.get('roster_id', '?')}"
                for r in self.rosters for key in ("players", "reserve", "taxi") for pid in (r.get(key) or [])}

    def weather_for(self, game: dict):
        gid = game["game_id"]
        if gid not in self._wx:
            try:
                self._wx[gid] = weather.forecast(game)
            except Exception as exc:
                print(f"[warn] weather failed for {gid}: {exc}")
                self._wx[gid] = None
        return self._wx[gid]


@dataclass
class Result:
    ctx: Context
    roster: list[PlayerEval]
    reserve: list[PlayerEval]
    lineup: list
    current_starters: list[str]
    waiver_recs: list
    candidates: list = field(default_factory=list)


def _log(msg):
    print(f"  - {msg}", flush=True)


def build_context(username: str, team_name: str, league_id: str | None = None, season: int | None = None,
                  week: int | None = None, cfg: dict | None = None) -> Context:
    """Fetch everything a week's evaluation needs (all network I/O happens here)."""
    cfg = cfg or load_config()
    state = sleeper.nfl_state()
    season = season or int(state["season"])
    week = week or int(state.get("display_week") or state["week"])
    _log(f"Season {season}, week {week}")

    lg, users, rosters, my_roster, my_user = sleeper.find_my_team(username, team_name, season, league_id)
    my_name = ((my_user.get("metadata") or {}).get("team_name") or my_user.get("display_name"))
    _log(f"League '{lg.get('name')}', team '{my_name}'")
    scoring = lg.get("scoring_settings") or {}
    rec_value = float(scoring.get("rec", 0))

    players = sleeper.all_players()
    projections = sleeper.projections(season, week)
    n_recent = cfg["recent_weeks"]
    recent = {w: sleeper.stats(season, w) for w in range(max(1, week - n_recent), week)}
    try:
        trending = sleeper.trending_adds()
    except Exception:
        trending = {}
    _log(f"Loaded {len(players)} players, {len(projections)} projections")

    try:
        stats = nflverse.weekly_stats(season)
    except Exception as exc:
        print(f"[warn] nflverse stats unavailable ({exc}); matchup ratings disabled")
        stats = pd.DataFrame()
    defense = nflverse.defense_vs_position(stats, week, rec_value, cfg.get("defense_lookback_weeks"))
    matchups, implied_avg = nflverse.matchups(nflverse.schedule(season), week)
    _log(f"Defense ratings for {defense['opponent_team'].nunique() if not defense.empty else 0} teams, "
         f"{len(matchups) // 2} games this week")

    return Context(cfg, season, week, lg, my_roster, my_name, scoring, rec_value, players,
                   projections, recent, defense, matchups, implied_avg, trending, users, rosters)


def run(username: str, team_name: str, league_id: str | None = None, season: int | None = None,
        week: int | None = None, cfg: dict | None = None, news: bool = True, wx: bool = True) -> Result:
    cfg = cfg or load_config()
    cfg["weather"]["enabled"] = cfg["weather"]["enabled"] and wx
    ctx = build_context(username, team_name, league_id, season, week, cfg)
    lg, my_roster = ctx.league, ctx.my_roster
    ev = Evaluator(ctx)

    reserve_ids = set(my_roster.get("reserve") or []) | set(my_roster.get("taxi") or [])
    active_ids = [p for p in (my_roster.get("players") or []) if p not in reserve_ids]
    roster = [ev.evaluate(pid) for pid in active_ids]
    reserve = [ev.evaluate(pid, with_weather=False) for pid in reserve_ids]

    # waiver candidates (evaluated without news first, then news for the top few)
    pool_ids = waivers.free_agent_pool(ctx, ctx.rostered, ctx.fantasy_positions)
    candidates = sorted((ev.evaluate(pid) for pid in pool_ids), key=lambda e: e.adj, reverse=True)
    top_candidates = candidates[: cfg["waivers"]["news_for_top"]]
    _log(f"Evaluated {len(roster)} rostered players and {len(candidates)} free agents")

    if news and cfg["news"]["enabled"]:
        scorer = NewsScorer(cfg["news"])
        targets = [e for e in roster + top_candidates if e.position != "DEF"]
        _log(f"Scoring news for {len(targets)} players...")
        sentiments = scorer.for_players([e.name for e in targets])
        for e in targets:
            ev.apply_sentiment(e, sentiments.get(e.name))

    lineup = optimal_lineup(roster, lg["roster_positions"])
    starters = {e.player_id for _, e in lineup if e}
    for slot, e in lineup:
        if e:
            e.slot = slot
    recs = waivers.recommend(ctx, ev, roster, starters, candidates)
    return Result(ctx, roster, reserve, lineup, my_roster.get("starters") or [], recs, candidates)
