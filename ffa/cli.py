"""Command line entry points.

    python -m ffa --username <sleeper_username>          # weekly report
    python -m ffa ledger --username <sleeper_username>   # transaction ledger
    python -m ffa ui --username <sleeper_username>       # local web UI
    python -m ffa calibrate [--seasons 2025,2026]        # projection calibration report
"""
from __future__ import annotations

import argparse
import sys

from . import ledger, pipeline, report, snapshot


def ledger_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="python -m ffa ledger",
                                 description="Multi-week value of every add/drop/trade in the league")
    ap.add_argument("--username")
    ap.add_argument("--team-name")
    ap.add_argument("--league-id")
    ap.add_argument("--through-week", type=int, help="Last completed week (default: current week - 1)")
    ap.add_argument("--config")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args(argv)
    cfg = pipeline.load_config(args.config)
    username = args.username or cfg.get("username")
    if not username:
        ap.error("--username is required (or set 'username' in config.json)")
    md, csv = ledger.run(username, args.team_name or cfg.get("team_name", ""),
                         args.league_id or cfg.get("league_id"), args.through_week, cfg, args.out)
    print(f"\nSaved {md} and {csv}")
    return 0


def calibrate_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="python -m ffa calibrate",
                                 description="Compare Sleeper projections (and simple alternatives) with actual points")
    ap.add_argument("--username", help="Score with your league's settings (otherwise Sleeper PPR totals)")
    ap.add_argument("--team-name")
    ap.add_argument("--league-id")
    ap.add_argument("--seasons", help="Comma-separated, e.g. 2025,2026 (default: current season)")
    ap.add_argument("--weeks", help="Range like 1-4 (default: all completed weeks)")
    ap.add_argument("--ppr", type=float, default=1.0, help="Points per reception when no username is given")
    ap.add_argument("--config")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args(argv)
    cfg = pipeline.load_config(args.config)
    from . import calibration, sleeper
    seasons = [int(s) for s in args.seasons.split(",")] if args.seasons else [int(sleeper.nfl_state()["season"])]
    weeks = None
    if args.weeks:
        lo, _, hi = args.weeks.partition("-")
        weeks = list(range(int(lo), int(hi or lo) + 1))
    username = args.username or cfg.get("username") or None
    md, csv = calibration.run(seasons, weeks, cfg, args.out, username, args.team_name or cfg.get("team_name", ""),
                              args.league_id or cfg.get("league_id"), args.ppr)
    print(md.read_text())
    print(f"\nSaved {md} and {csv}")
    return 0


def ui_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="python -m ffa ui", description="Local web UI for the analyzer")
    ap.add_argument("--username")
    ap.add_argument("--team-name")
    ap.add_argument("--league-id")
    ap.add_argument("--week", type=int, help="Defaults to the current NFL week")
    ap.add_argument("--config")
    ap.add_argument("--out", default="reports", help="Folder holding run snapshots and reports")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    cfg = pipeline.load_config(args.config)
    username = args.username or cfg.get("username")
    if not username:
        ap.error("--username is required (or set 'username' in config.json)")
    from . import server  # fastapi/uvicorn only needed for the UI
    server.serve(username, args.team_name or cfg.get("team_name", ""), args.league_id or cfg.get("league_id"),
                 args.week, cfg, args.out, args.host, args.port)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "ledger":
        return ledger_main(argv[1:])
    if argv and argv[0] == "ui":
        return ui_main(argv[1:])
    if argv and argv[0] == "calibrate":
        return calibrate_main(argv[1:])
    ap = argparse.ArgumentParser(description="Weekly Sleeper start/sit and waiver analyzer")
    ap.add_argument("--username", help="Sleeper username (or set in config.json)")
    ap.add_argument("--team-name", help="Team name to analyze (default from config: Brendobendo)")
    ap.add_argument("--league-id", help="Sleeper league id (optional; otherwise searched)")
    ap.add_argument("--season", type=int)
    ap.add_argument("--week", type=int, help="Defaults to the current NFL week")
    ap.add_argument("--config", help="Path to config.json")
    ap.add_argument("--no-news", action="store_true", help="Skip news scraping/sentiment")
    ap.add_argument("--no-weather", action="store_true", help="Skip weather forecasts")
    ap.add_argument("--out", default="reports", help="Output folder")
    args = ap.parse_args(argv)

    cfg = pipeline.load_config(args.config)
    username = args.username or cfg.get("username")
    if not username:
        ap.error("--username is required (or set 'username' in config.json)")
    print("Running fantasy analysis...")
    with snapshot.capture() as lines:
        res = pipeline.run(
            username=username,
            team_name=args.team_name or cfg.get("team_name", ""),
            league_id=args.league_id or cfg.get("league_id"),
            season=args.season, week=args.week, cfg=cfg,
            news=not args.no_news, wx=not args.no_weather,
        )
    print()
    print(report.render(res))
    md, csv = report.save(res, args.out)
    opts = {"news": not args.no_news, "weather": not args.no_weather}
    run_dir = snapshot.save(snapshot.to_dict(res, snapshot.warnings_from(lines), opts), args.out, (md, csv),
                            cfg.get("archive_dir", "archive"))
    print(f"\nSaved {md} and {csv}; archived run to {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
