"""Transaction ledger (design spec §4.5, "Pickups pay off over several weeks").

For every completed add (waiver, free agent, or one side of a trade) by any team, and
for every week the added player stays on that roster:

    value(week) = best lineup from the actual roster
                - best lineup from the roster with the move undone (adds removed, drops restored)

Both sides use actual points and an exact hindsight optimizer, so the value measures the
move itself, independent of who was started that week. Everything is rebuilt from Sleeper's
historical endpoints (transactions + matchups + stats), so no prior logging is needed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import sleeper
from .lineup import best_total, player_positions

ADD_TYPES = {"waiver", "free_agent"}


@dataclass
class WeekData:
    rosters: dict[int, set[str]]   # roster_id -> players on the roster that week
    points: dict[str, float]       # player_id -> actual fantasy points (league scoring)


@dataclass
class LedgerEntry:
    transaction_id: str
    week: int                      # Sleeper "leg" the move was made in
    type: str                      # waiver | free_agent | trade
    roster_id: int
    adds: list[str]
    drops: list[str]
    bid: int | None = None
    runner_up_bid: int | None = None
    weekly: dict[int, float] = field(default_factory=dict)

    @property
    def weeks_active(self) -> int:
        return len(self.weekly)

    def _first(self, n: int) -> float:
        return round(sum(v for _, v in sorted(self.weekly.items())[:n]), 2)

    @property
    def value_1wk(self) -> float:
        return self._first(1)

    @property
    def value_3wk(self) -> float:
        return self._first(3)

    @property
    def value_total(self) -> float:
        return round(sum(self.weekly.values()), 2)

    @property
    def pts_per_dollar(self) -> float | None:
        return round(self.value_total / self.bid, 2) if self.bid else None

    @property
    def overpay_ratio(self) -> float | None:
        if self.bid and self.runner_up_bid:
            return round(self.bid / self.runner_up_bid, 2)
        return None


# ---- pure logic ----------------------------------------------------------------

def _bid(t: dict) -> int | None:
    b = (t.get("settings") or {}).get("waiver_bid")
    return int(b) if b is not None else None


def build_ledger(txns_by_week: dict[int, list[dict]], weeks: dict[int, WeekData],
                 roster_positions: list[str], positions: dict[str, set[str]]) -> list[LedgerEntry]:
    """Value every completed add across the weeks in `weeks` (completed weeks only)."""
    if not weeks:
        return []
    last = max(weeks)

    # highest losing bid per (week, player) -> runner-up for the winning claim
    losing: dict[tuple[int, str], int] = {}
    for w, txs in txns_by_week.items():
        for t in txs:
            if t.get("type") == "waiver" and t.get("status") == "failed" and _bid(t) is not None:
                for pid in t.get("adds") or {}:
                    losing[(w, pid)] = max(losing.get((w, pid), 0), _bid(t))

    entries = []
    for w in sorted(txns_by_week):
        if w > last:
            continue
        for t in sorted(txns_by_week[w], key=lambda t: t.get("status_updated") or t.get("created") or 0):
            if t.get("status") != "complete":
                continue
            adds, drops = t.get("adds") or {}, t.get("drops") or {}
            for rid in sorted({*adds.values(), *drops.values()}):
                a = [p for p, r in adds.items() if r == rid]
                if not a:          # pure drops aren't valued yet
                    continue
                d = [p for p, r in drops.items() if r == rid]
                e = LedgerEntry(str(t.get("transaction_id")), w, t.get("type", "?"), rid, a, d)
                if t.get("type") == "waiver":
                    e.bid = _bid(t)
                    e.runner_up_bid = max((losing.get((w, p), 0) for p in a), default=0) or None
                _value_weeks(e, weeks, last, roster_positions, positions)
                entries.append(e)
    return entries


def _value_weeks(e: LedgerEntry, weeks: dict[int, WeekData], last: int,
                 roster_positions: list[str], positions: dict[str, set[str]]) -> None:
    started = False
    for wk in range(e.week, last + 1):
        if wk not in weeks:
            continue
        roster = weeks[wk].rosters.get(e.roster_id, set())
        present = [p for p in e.adds if p in roster]
        if not present:
            # a move made late in week N may first show up on week N+1's roster
            if started or wk > e.week + 1:
                break
            continue
        started = True
        pts = weeks[wk].points
        with_move = best_total(roster, pts, positions, roster_positions)
        without = best_total((roster - set(present)) | set(e.drops), pts, positions, roster_positions)
        e.weekly[wk] = round(with_move - without, 2)


def moves_per_team(txns_by_week: dict[int, list[dict]], roster_ids: list[int],
                   weeks: list[int]) -> pd.DataFrame:
    """One row per (roster, week) with the number of players added via waivers/free agency."""
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for w, txs in txns_by_week.items():
        for t in txs:
            if t.get("status") == "complete" and t.get("type") in ADD_TYPES:
                for rid in (t.get("adds") or {}).values():
                    counts[(rid, w)] += 1
    rows = [{"roster_id": r, "week": w, "adds": counts.get((r, w), 0)} for r in roster_ids for w in weeks]
    return pd.DataFrame(rows, columns=["roster_id", "week", "adds"])


def max_adds_check(moves: pd.DataFrame, max_adds: int, top_roster_ids: set[int]) -> dict:
    """Empirical gut check for eval.max_adds (spec Q6)."""
    if moves.empty:
        return {}
    a = moves["adds"]
    per_team = moves.groupby("roster_id")["adds"].mean()
    top = moves[moves["roster_id"].isin(top_roster_ids)]["adds"]
    return {
        "max_adds": max_adds,
        "team_weeks": int(len(a)),
        "mean": round(float(a.mean()), 2),
        "p75": float(a.quantile(0.75)),
        "p90": float(a.quantile(0.90)),
        "max": int(a.max()),
        "share_over_max_adds": round(float((a > max_adds).mean()), 3),
        "top_half_mean": round(float(top.mean()), 2) if len(top) else None,
        "busiest_team_mean": round(float(per_team.max()), 2),
    }


# ---- loading from Sleeper --------------------------------------------------------

def load_history(lg: dict, through_week: int, players: dict) -> tuple[dict, dict]:
    """Fetch transactions and per-week rosters/points for weeks 1..through_week."""
    lid, season = lg["league_id"], int(lg["season"])
    scoring = lg.get("scoring_settings") or {}
    rec = float(scoring.get("rec", 0))
    txns, weeks = {}, {}
    for w in range(1, through_week + 1):
        txns[w] = sleeper.transactions(lid, w)
        # computed points cover free agents/dropped players; matchup points (Sleeper's own
        # league scoring) override them for anyone rostered
        pts = {pid: sleeper.fantasy_points(line, scoring, rec, (players.get(pid) or {}).get("position", "")) or 0.0
               for pid, line in sleeper.stats(season, w).items()}
        rosters = {}
        for m in sleeper.matchups(lid, w):
            rosters[int(m["roster_id"])] = set(m.get("players") or [])
            pts.update({pid: float(v) for pid, v in (m.get("players_points") or {}).items()})
        weeks[w] = WeekData(rosters, pts)
    return txns, weeks


def run(username: str, team_name: str, league_id: str | None, through_week: int | None,
        cfg: dict, out_dir: str | Path) -> tuple[Path, Path]:
    state = sleeper.nfl_state()
    season = int(state["season"])
    lg, users, rosters, my_roster, _ = sleeper.find_my_team(username, team_name, season, league_id)
    through = through_week or max(0, int(state["week"]) - 1)
    print(f"  - Ledger for '{lg.get('name')}', weeks 1-{through}", flush=True)
    players = sleeper.all_players()
    txns, weeks = load_history(lg, through, players)

    names = {u["user_id"]: sleeper._team_name(u) for u in users}
    teams = {int(r["roster_id"]): names.get(r.get("owner_id"), f"Roster {r['roster_id']}") for r in rosters}
    entries = build_ledger(txns, weeks, lg["roster_positions"], player_positions(players))

    moves = moves_per_team(txns, sorted(teams), list(range(1, through + 1)))
    fpts = {int(r["roster_id"]): (r.get("settings") or {}).get("fpts", 0) for r in rosters}
    top = set(sorted(fpts, key=fpts.get, reverse=True)[: max(1, len(fpts) // 2)])
    check = max_adds_check(moves, cfg.get("eval", {}).get("max_adds", 3), top)

    def pname(pid):
        p = players.get(pid) or {}
        return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid

    md = render(entries, teams, int(my_roster["roster_id"]), moves, check, pname, season, through)
    out = Path(out_dir) / "ledger"
    out.mkdir(parents=True, exist_ok=True)
    stem = f"ledger_{season}_thru_wk{through:02d}"
    md_path, csv_path = out / f"{stem}.md", out / f"{stem}.csv"
    md_path.write_text(md)
    to_frame(entries, teams, pname).to_csv(csv_path, index=False)
    print(md)
    return md_path, csv_path


# ---- output ----------------------------------------------------------------------

def to_frame(entries: list[LedgerEntry], teams: dict[int, str], pname) -> pd.DataFrame:
    return pd.DataFrame([{
        "transaction_id": e.transaction_id, "week": e.week, "type": e.type, "roster_id": e.roster_id,
        "team": teams.get(e.roster_id, e.roster_id), "added": "; ".join(map(pname, e.adds)),
        "dropped": "; ".join(map(pname, e.drops)), "bid": e.bid, "runner_up_bid": e.runner_up_bid,
        "weeks_active": e.weeks_active, "value_1wk": e.value_1wk, "value_3wk": e.value_3wk,
        "value_total": e.value_total, "pts_per_dollar": e.pts_per_dollar, "overpay_ratio": e.overpay_ratio,
        "weekly": " ".join(f"w{w}:{v:+.1f}" for w, v in sorted(e.weekly.items())),
    } for e in entries])


def _t(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(out + ["| " + " | ".join("-" if c is None else str(c) for c in r) + " |" for r in rows])


def render(entries, teams, my_rid, moves, check, pname, season, through) -> str:
    f = lambda v: "-" if v is None else f"{v:+.1f}"
    L = [f"# Transaction ledger - {season}, through week {through}", "",
         "_Value = best hindsight lineup with the move minus best hindsight lineup with it undone, "
         "summed over the weeks the added player stayed on the roster._", ""]

    mine = [e for e in entries if e.roster_id == my_rid]
    L += ["## My moves", ""]
    if mine:
        L += [_t(["Wk", "Type", "Added", "Dropped", "Bid", "Runner-up", "Wks", "1 wk", "3 wk", "Total", "Pts/$"],
                 [[e.week, e.type, ", ".join(map(pname, e.adds)), ", ".join(map(pname, e.drops)) or "-",
                   e.bid, e.runner_up_bid, e.weeks_active, f(e.value_1wk), f(e.value_3wk), f(e.value_total),
                   e.pts_per_dollar] for e in mine]),
              "", f"**Net value of my moves: {sum(e.value_total for e in mine):+.1f} pts**", ""]
        over = [e for e in mine if e.overpay_ratio and e.overpay_ratio > 2]
        if over:
            L += [f"> Bid more than 2x the runner-up on: {', '.join(pname(e.adds[0]) for e in over)}", ""]
    else:
        L += ["No completed adds yet.", ""]

    L += ["## Best pickups in the league", ""]
    best = sorted(entries, key=lambda e: e.value_total, reverse=True)[:15]
    L += [_t(["Team", "Wk", "Type", "Added", "Dropped", "Bid", "Total", "Pts/$"],
             [[teams.get(e.roster_id), e.week, e.type, ", ".join(map(pname, e.adds)),
               ", ".join(map(pname, e.drops)) or "-", e.bid, f(e.value_total), e.pts_per_dollar] for e in best]), ""]

    L += ["## Team totals", ""]
    rows = []
    for rid, name in teams.items():
        es = [e for e in entries if e.roster_id == rid]
        spent = sum(e.bid or 0 for e in es)
        val = sum(e.value_total for e in es)
        adds = int(moves[moves["roster_id"] == rid]["adds"].sum()) if not moves.empty else 0
        rows.append([name + (" (me)" if rid == my_rid else ""), adds, f(val), spent or "-",
                     round(val / spent, 2) if spent else "-"])
    rows.sort(key=lambda r: float(r[2]) if r[2] != "-" else 0, reverse=True)
    L += [_t(["Team", "Adds", "Ledger value", "FAAB spent", "Pts/$"], rows), ""]

    if check:
        L += ["## How many adds per week? (max_adds check)", "",
              _t(["Team-weeks", "Mean", "75th pct", "90th pct", "Max", "Top-half teams mean",
                  "Busiest team mean", f"Share over {check['max_adds']}"],
                 [[check["team_weeks"], check["mean"], check["p75"], check["p90"], check["max"],
                   check["top_half_mean"], check["busiest_team_mean"], f"{check['share_over_max_adds']:.0%}"]]), ""]
        verdict = ("max_adds looks generous enough"
                   if check["p90"] <= check["max_adds"] else "consider raising max_adds")
        L += [f"_eval.max_adds = {check['max_adds']}: {verdict} (90th percentile team-week = {check['p90']:g})._", ""]
    return "\n".join(L)
