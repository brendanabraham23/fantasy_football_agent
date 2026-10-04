# Fantasy Football Analyzer (Sleeper)

Weekly start/sit and waiver-wire recommendations for **Team Brendobendo**.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m ffa --username YOUR_SLEEPER_USERNAME
```

This prints the report and saves `reports/weekNN_YYYY_report.md` plus a CSV with every
signal for every player. Put your username (and optionally `league_id`) in `config.json`
to skip the flag. Python 3.10+.

Useful flags: `--week 6`, `--league-id 123...`, `--team-name "Other Team"`,
`--no-news` (fast run), `--no-weather`.

## What it does

| Step | Source | Module |
|---|---|---|
| 1. League, rosters, scoring settings, FAAB budget | Sleeper API (official v1) | `sleeper.py` |
| 2. Find Team Brendobendo | league users' `team_name`, fallback to your own roster | `sleeper.find_my_team` |
| 3. News articles per player | Google News RSS (per player, last 7 days) + ESPN / CBS / PFT / Yahoo RSS | `news.py` |
| 4. Sentiment score | VADER with a football lexicon + phrase rules ("ruled out", "full practice", "lost the job"...), recency-weighted, shrunk toward 0 when few articles | `news.py` |
| 5a. Injury status | Sleeper player data (Questionable / Doubtful / Out / IR...) | `sleeper.py` |
| 5b. Defense rankings | Fantasy points each defense allows per position this season, from nflverse weekly stats | `nflverse.py` |
| 5c. Vegas implied team totals | nflverse schedule (spread + total) | `nflverse.py` |
| 5d. Game-day weather | Open-Meteo forecast at the stadium over kickoff +3h; domes skipped | `weather.py` |
| 6. Rank and pick a lineup | see below | `ranker.py` |
| Waivers | Free agents (top projected per position + Sleeper trending adds) | `waivers.py` |

### Why not the `fantasyfootball` package?

The data bundled with it ends at the 2022 season, the repo hasn't been updated since mid-2024, and
it only installs on Python 3.10 or older. nflverse publishes the same kinds of data
(stats, schedule, lines, stadium roof) for 2026 and updates nightly.

## How players are ranked

Everything is expressed in **adjusted fantasy points** in your league's own scoring:

```
base = 0.7 x Sleeper projection + 0.3 x average of last 3 games
adj  = base x injury x matchup x weather x sentiment
```

- **injury**: Questionable 0.85, Doubtful 0.25, Out/IR/Sus 0. A bye week counts as 0.
- **matchup**: `1 + 0.5 x (defense's points allowed to this position / league average - 1)`, then
  multiplied by `1 + 0.5 x (team implied total / week average - 1)`. For DEF, the opponent's implied
  total works in reverse. Clipped to 0.8-1.2.
- **weather**: wind over 15 mph costs QB/WR/TE about 3% and K about 6% per 5 mph; heavy rain or snow trims
  passing and kicking and helps RB/DEF slightly. Clipped to 0.75-1.1.
- **sentiment**: `1 + 0.08 x score`, score in [-1, 1].

The lineup fills dedicated slots first (QB, RB, WR, TE, K, DEF), then FLEX / SUPER_FLEX from
what's left, and lists the moves needed relative to the lineup you currently have set in Sleeper.

**Waivers**: each candidate is added to your roster in a what-if run and scored on
(a) how much this week's best lineup total goes up, and (b) longer-term value (average of projection
and recent form) vs. your weakest bench player, who is suggested as the drop. K/DEF are compared
like-for-like for streaming. The FAAB bid suggestion scales with both gains and Sleeper add volume, capped
at 35% of your remaining budget. Treat it as a starting point.

Every number above is in `config.json`, so you can tune them.

## Caveats

- Sleeper's weekly **projections/stats** come from `api.sleeper.com`, the endpoint the Sleeper app uses.
  It's undocumented, so it could change without notice.
- Google News can rate-limit heavy use. Results are cached for 3 hours in `~/.cache/ffa`. Delete that folder to force a refresh.
- Defense ratings are thin early in the season (only a few games). Set `defense_lookback_weeks`
  (e.g. 4) later in the year to weight recent form.
- International/neutral-site games skip the weather forecast.

## Transaction ledger

```bash
python -m ffa ledger --username YOUR_SLEEPER_USERNAME [--through-week 4]
```

Scores every completed add, drop and trade by every team in the league, using only completed weeks.
For each week the added player stays on the roster, it takes the best hindsight lineup with the move
and subtracts the best hindsight lineup with the move undone. The report covers your moves (with FAAB
points per dollar and how your bid compared with the runner-up), the league's best pickups, team totals,
and an adds-per-week distribution that checks `eval.max_adds`. Output goes to `reports/ledger/`.
Design: `docs/eval-loop-design.md`.

## Tests

```bash
pytest -q
```
All tests run offline using fixture data.

## Ideas for next steps

- Schedule it Wednesday (after waivers clear) and Sunday morning (final injury news).
- Pull rest-of-season schedule strength into the waiver score.
- Run the report for every team in the league to spot trade targets.
