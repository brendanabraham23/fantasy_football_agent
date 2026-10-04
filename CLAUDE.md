# CLAUDE.md

Weekly start/sit and waiver recommendations for a Sleeper fantasy league (team "Brendobendo"), plus a
transaction ledger that scores every add/drop/trade in hindsight. Pure Python package `ffa/`, no web UI.

## Commands

```bash
source .venv/bin/activate              # Python 3.13 venv already exists; deps in requirements.txt
python -m pytest -q                    # all tests, offline. Use `python -m pytest`, NOT bare `pytest`:
                                       # bare pytest doesn't put the repo root on sys.path -> "No module named 'ffa'"
python -m ffa --username USER          # weekly report -> reports/weekNN_YYYY_report.md + _players.csv
python -m ffa --username USER --no-news --no-weather   # fast run, skips RSS scraping and Open-Meteo
python -m ffa ledger --username USER [--through-week 4] # -> reports/ledger/
```

`username` is blank in `config.json`, so live runs need `--username` (ask the user for it).

## Architecture

`cli.py` -> `pipeline.run()` builds a `Context` (league, rosters, projections, recent stats, defense
ratings, matchups) -> `ranker.Evaluator.evaluate(pid)` returns a `PlayerEval` -> `ranker.optimal_lineup`
-> `waivers.recommend` -> `report.render/save`.

| Module | Role |
|---|---|
| `http.py` | Single `requests` session + on-disk cache (`~/.cache/ffa`, override with `FFA_CACHE_DIR`). All network goes through `http.get/get_json/get_text` with a `ttl`. |
| `sleeper.py` | Sleeper v1 API (league, users, rosters, transactions) + undocumented `api.sleeper.com` projections/stats. `norm_team` maps Sleeper team codes to nflverse ones. |
| `nflverse.py` | nflverse release CSVs: schedule/Vegas lines, weekly stats -> defense-vs-position ratings. |
| `news.py` | Google News + generic NFL RSS -> VADER sentiment with football lexicon/phrase rules. |
| `weather.py` | Open-Meteo forecast at stadium; domes and neutral sites skipped. |
| `ranker.py` | `adj = base x injury x matchup x weather x sentiment`; lineup optimizer for the weekly report. |
| `waivers.py` | What-if add of each free agent; weekly gain + ROS gain vs. weakest bench; FAAB bid. |
| `lineup.py` | Hindsight best-lineup solver used by `ledger.py` (separate from `ranker.optimal_lineup`). |
| `ledger.py` | Values each transaction as (best lineup with move) - (best lineup with move undone), per week held. |

All tunable numbers (weights, clips, injury multipliers, news/waiver params) live in `config.json`;
don't hardcode new constants in modules — add a config key and read it from `cfg`.

## Conventions

- `from __future__ import annotations`, dataclasses, module docstring on every file. Terse style, few comments.
- Network failures in optional signals (weather, trending, nflverse) degrade with a `[warn]` print rather than
  aborting the run. Keep that behavior for new signals.
- Tests in `tests/` monkeypatch every network call with fixture data (see the `fake_world` fixture in
  `tests/test_ffa.py`). New code that fetches data must go through `http.py` / the source module so it can
  be patched; tests must stay offline.

## Roadmap

`docs/eval-loop-design.md` is the design for the next phase (prediction log, actuals ingestion, evaluation
metrics, error attribution, backtesting, GitHub Actions schedule). Only the ledger part is implemented.
Read it before working on evaluation/backtesting features; planned modules are listed in its §4.11.

## Repo notes

- `reports/` is generated output; the design doc plans to keep reports off `main` (on a `data` branch).
- `fantasy_football_analyzer.zip` is an older snapshot of this same code; not part of the source.
- `.env` is gitignored; never commit it.
