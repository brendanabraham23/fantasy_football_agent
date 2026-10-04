"""Turn raw signals into an adjusted point estimate per player, then build a lineup."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import sleeper, weather

SLOT_ELIGIBILITY = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "WRRB_FLEX": {"RB", "WR"}, "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
NON_STARTING = {"BN", "IR", "TAXI"}


def player_name(p: dict, pid: str) -> str:
    return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid


@dataclass
class PlayerEval:
    player_id: str
    name: str
    position: str
    team: str | None
    opp: str | None = None
    is_home: bool | None = None
    on_bye: bool = False
    injury_status: str | None = None
    injury_detail: str | None = None
    proj: float | None = None
    recent_avg: float | None = None
    recent_games: int = 0
    def_rank: int | None = None
    def_teams: int | None = None
    def_ratio: float | None = None
    implied: float | None = None
    weather: str | None = None
    sentiment: float = 0.0
    n_articles: int = 0
    headlines: list = field(default_factory=list)
    articles: list = field(default_factory=list)
    mult_injury: float = 1.0
    mult_matchup: float = 1.0
    mult_weather: float = 1.0
    mult_sentiment: float = 1.0
    base: float = 0.0
    adj: float = 0.0
    trending_adds: int = 0
    slot: str | None = None
    notes: list = field(default_factory=list)

    @property
    def matchup_grade(self) -> str:
        if not self.def_rank or not self.def_teams:
            return "-"
        pct = self.def_rank / self.def_teams  # high rank = generous defense
        return "A" if pct > 0.8 else "B" if pct > 0.6 else "C" if pct > 0.4 else "D" if pct > 0.2 else "F"

    @property
    def ros_value(self) -> float:
        vals = [v for v in (self.proj, self.recent_avg) if v is not None]
        return sum(vals) / len(vals) if vals else 0.0


class Evaluator:
    def __init__(self, ctx):
        self.c = ctx

    def evaluate(self, pid: str, with_weather: bool = True) -> PlayerEval:
        c, cfg = self.c, self.c.cfg
        p = c.players.get(pid, {})
        pos = p.get("position") or "?"
        name = player_name(p, pid)
        team = sleeper.norm_team(p.get("team"))
        e = PlayerEval(pid, name, pos, team, injury_status=p.get("injury_status"),
                       injury_detail=p.get("injury_body_part") or p.get("injury_notes"),
                       trending_adds=c.trending.get(pid, 0))

        e.proj = sleeper.fantasy_points(c.projections.get(pid), c.scoring, c.rec_value, pos)
        games = [pts for wk in c.recent.values()
                 if (line := wk.get(pid)) and (line.get("gp", 1) or 0) > 0
                 and (pts := sleeper.fantasy_points(line, c.scoring, c.rec_value, pos)) is not None]
        if games:
            e.recent_avg, e.recent_games = round(sum(games) / len(games), 2), len(games)

        game = c.matchups.get(team)
        if team and not game:
            e.on_bye = True
            e.notes.append("BYE")
        if game:
            e.opp, e.is_home, e.implied = game["opp"], game["is_home"], game["implied"]

        # base points: blend projection with recent form
        w = cfg["weights"]
        if e.proj is not None and e.recent_avg is not None:
            e.base = w["projection"] * e.proj + w["recent_form"] * e.recent_avg
        else:
            e.base = e.proj if e.proj is not None else (e.recent_avg or 0.0)

        # injury
        e.mult_injury = 0.0 if e.on_bye else cfg["injury_multipliers"].get(e.injury_status or "", 1.0)
        if e.injury_status:
            e.notes.append(f"{e.injury_status}" + (f" ({e.injury_detail})" if e.injury_detail else ""))

        # matchup: defense vs position + Vegas implied total
        m = 1.0
        if game:
            row = c.defense[(c.defense["opponent_team"] == e.opp) & (c.defense["position"] == pos)] \
                if not c.defense.empty else pd.DataFrame()
            if not row.empty:
                r = row.iloc[0]
                e.def_rank, e.def_teams, e.def_ratio = int(r["rank"]), int(r["n_teams"]), round(float(r["ratio"]), 2)
                m *= 1 + cfg["matchup_weight"] * (e.def_ratio - 1)
            if pos == "DEF" and game.get("opp_implied") is not None:
                m *= 1 - cfg["vegas_weight"] * (game["opp_implied"] / c.implied_avg - 1)
            elif e.implied is not None and pos != "DEF":
                m *= 1 + cfg["vegas_weight"] * (e.implied / c.implied_avg - 1)
        lo, hi = cfg["matchup_clip"]
        e.mult_matchup = round(max(lo, min(hi, m)), 3)

        # weather
        if with_weather and game and cfg["weather"]["enabled"]:
            wx = c.weather_for(game)
            if wx:
                e.weather = wx.get("summary")
                e.mult_weather = round(weather.multiplier(pos, wx), 3)

        self.apply_sentiment(e, None)
        return e

    def apply_sentiment(self, e: PlayerEval, sent) -> None:
        if sent is not None:
            e.sentiment, e.n_articles, e.headlines, e.articles = sent.score, sent.n_articles, sent.headlines, sent.articles
        e.mult_sentiment = round(1 + self.c.cfg["sentiment_weight"] * e.sentiment, 3)
        e.adj = round(e.base * e.mult_injury * e.mult_matchup * e.mult_weather * e.mult_sentiment, 2)


def optimal_lineup(evals: list[PlayerEval], roster_positions: list[str]) -> list[tuple[str, PlayerEval | None]]:
    """Greedy fill: dedicated slots first, then the most restrictive flex slots."""
    slots = [s for s in roster_positions if s not in NON_STARTING and s in SLOT_ELIGIBILITY]
    slots.sort(key=lambda s: len(SLOT_ELIGIBILITY[s]))
    pool = sorted(evals, key=lambda e: e.adj, reverse=True)
    used, lineup = set(), []
    for slot in slots:
        pick = next((e for e in pool if e.player_id not in used and e.position in SLOT_ELIGIBILITY[slot]), None)
        if pick:
            used.add(pick.player_id)
        lineup.append((slot, pick))
    order = {s: i for i, s in enumerate(roster_positions)}
    lineup.sort(key=lambda t: order.get(t[0], 99))
    return lineup


def lineup_total(evals: list[PlayerEval], roster_positions: list[str]) -> float:
    return round(sum(e.adj for _, e in optimal_lineup(evals, roster_positions) if e), 2)
