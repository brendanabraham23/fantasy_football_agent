"""Command line entry points.

    python -m ffa --username <sleeper_username>          # weekly report
    python -m ffa ledger --username <sleeper_username>   # transaction ledger
"""
from __future__ import annotations

import argparse
import sys

from . import ledger, pipeline, report


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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "ledger":
        return ledger_main(argv[1:])
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
    print(f"\nSaved {md} and {csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
