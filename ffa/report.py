"""Render a weekly report as Markdown (also printed to the console) and a CSV."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

from .pipeline import Result
from .ranker import PlayerEval

POS_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]


def _f(v, nd=1):
    return "-" if v is None else f"{v:.{nd}f}"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _opp(e: PlayerEval) -> str:
    if e.on_bye:
        return "BYE"
    if not e.opp:
        return "-"
    return ("vs " if e.is_home else "@ ") + e.opp


def _row(e: PlayerEval) -> list:
    rank = f"{e.def_rank}/{e.def_teams}" if e.def_rank else "-"
    return [e.name, e.position, e.team or "FA", _opp(e), _f(e.proj), _f(e.recent_avg),
            f"{e.matchup_grade} ({rank})", _f(e.implied), e.weather or "-",
            f"{e.sentiment:+.2f} ({e.n_articles})", e.injury_status or "-", f"**{_f(e.adj)}**"]


HEAD = ["Player", "Pos", "Team", "Opp", "Proj", "Recent", "Matchup (def rank)", "Implied",
        "Weather", "Sentiment (n)", "Injury", "Adj pts"]


def render(res: Result) -> str:
    c = res.ctx
    lines = [f"# {c.my_team_name} - Week {c.week}, {c.season}",
             f"_{c.league.get('name')} - generated {datetime.now():%a %b %d %I:%M %p}_", ""]

    # lineup
    lines += ["## Recommended lineup", ""]
    rows = []
    for slot, e in res.lineup:
        rows.append([slot] + (_row(e) if e else ["(empty)"] + [""] * (len(HEAD) - 1)))
    lines += [_table(["Slot"] + HEAD, rows), ""]
    total = sum(e.adj for _, e in res.lineup if e)
    lines += [f"Projected lineup total (adjusted): **{total:.1f}**", ""]
    holes = [(slot, e) for slot, e in res.lineup if e is None or e.adj <= 0]
    for slot, e in holes:
        who = f"{e.name} ({', '.join(e.notes) or 'no projection'})" if e else "nobody eligible"
        lines.append(f"> **No healthy {slot} on your roster** - {who}. Check the waiver targets below.")
    if holes:
        lines.append("")

    # changes vs current Sleeper lineup
    optimal = {e.player_id for _, e in res.lineup if e}
    current = {p for p in res.current_starters if p and p != "0"}
    by_id = {e.player_id: e for e in res.roster + res.reserve}
    ins = [by_id[p] for p in optimal - current if p in by_id]
    outs = [by_id[p] for p in current - optimal if p in by_id]
    if ins or outs:
        lines += ["### Changes vs. your current Sleeper lineup", ""]
        lines += [f"- START **{e.name}** ({e.position}, {_f(e.adj)} adj)" for e in ins]
        lines += [f"- BENCH {e.name} ({e.position}, {_f(e.adj)} adj"
                  + (f", {', '.join(e.notes)}" if e.notes else "") + ")" for e in outs]
        lines.append("")
    else:
        lines += ["Your current Sleeper lineup already matches the recommendation.", ""]

    # alerts
    alerts = [e for e in res.roster if e.on_bye or e.mult_injury < 1]
    if alerts:
        lines += ["## Injury and bye alerts", ""]
        lines += [f"- {e.name} ({e.position}, {e.team}): {', '.join(e.notes)}" for e in alerts]
        lines.append("")

    # rankings by position
    lines += ["## Roster rankings by position", ""]
    for pos in POS_ORDER + sorted({e.position for e in res.roster} - set(POS_ORDER)):
        group = sorted((e for e in res.roster if e.position == pos), key=lambda e: e.adj, reverse=True)
        if group:
            lines += [f"### {pos}", "", _table(["#"] + HEAD, [[i + 1] + _row(e) for i, e in enumerate(group)]), ""]
    if res.reserve:
        lines += ["_On IR/taxi: " + ", ".join(f"{e.name} ({e.injury_status or 'reserve'})" for e in res.reserve) + "_", ""]

    # waivers
    lines += ["## Waiver wire targets", ""]
    if res.waiver_recs:
        rows = [[r.player.name, r.player.position, r.player.team, _opp(r.player), _f(r.player.adj),
                 _f(r.player.recent_avg), f"{r.weekly_gain:+.1f}", f"{r.ros_gain:+.1f}",
                 r.player.trending_adds or "-", r.drop.name if r.drop else "-", r.bid]
                for r in res.waiver_recs]
        lines += [_table(["Player", "Pos", "Team", "Opp", "Adj pts", "Recent", "Lineup gain (wk)",
                          "Value vs drop", "Adds (48h)", "Suggested drop", "Bid"], rows), ""]
        lines += ["_Lineup gain = change in this week's optimal lineup total. Value vs drop compares "
                  "the average of projection and recent form against the suggested drop._", ""]
    else:
        lines += ["No free agents clear the thresholds in config.json this week.", ""]

    # headlines
    lines += ["## News driving sentiment", ""]
    for e in sorted(res.roster, key=lambda e: abs(e.sentiment), reverse=True):
        if e.headlines:
            lines.append(f"**{e.name}** ({e.sentiment:+.2f})")
            lines += [f"- [{t}]({link}) ({s:+.2f})" for t, s, link in e.headlines]
            lines.append("")
    return "\n".join(lines)


def save(res: Result, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"week{res.ctx.week:02d}_{res.ctx.season}"
    md = out / f"{stem}_report.md"
    md.write_text(render(res))
    rows = [{**asdict(e), "group": "roster"} for e in res.roster]
    rows += [{**asdict(r.player), "group": "waiver", "weekly_gain": r.weekly_gain,
              "ros_gain": r.ros_gain, "bid": r.bid} for r in res.waiver_recs]
    csv = out / f"{stem}_players.csv"
    pd.DataFrame(rows).drop(columns=["headlines", "articles"]).to_csv(csv, index=False)
    return md, csv
