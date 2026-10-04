# Design: Local Web UI

## 1. Summary

A local web app (`python -m ffa ui`) that exposes every part of the analyzer in five tabs:
**Summary, Roster, Player, Waivers, News**. A FastAPI backend serves a JSON API and a
no-build HTML/JS frontend. Pages read a saved snapshot of the latest pipeline run. A
**Run pipeline** button starts a fresh run in the background. The player and waiver-browse
pages also read live Sleeper data through the existing on-disk HTTP cache, because the
snapshot doesn't hold full season histories or the full free-agent pool.

## 2. Goals and non-goals

Goals
- One place to answer the four weekly questions: *what do I change before kickoff, why does the model
  like/dislike each of my players, who should I pick up, and what is the news saying.*
- Explain the number: every adjusted-points value can be broken down into its multipliers.
- Browse the **full** waiver pool, not just the top-N the pipeline evaluates.
- Keep the CLI and tests working unchanged; tests stay offline.

Non-goals
- Hosting, auth, multi-user. The server binds to `127.0.0.1` by default.
- Writing to Sleeper (its API is read-only).
- The eval-loop features in `eval-loop-design.md` (prediction store, backtests). The snapshot format
  here is a stepping stone, not a replacement for that store.

## 3. Architecture

```
browser (ffa/web: index.html, app.js, style.css; hash routing, inline-SVG charts)
   │  fetch /api/*
FastAPI app (ffa/server.py)
   ├── snapshot  ── reports/latest_run.json      (written by every pipeline run, CLI or UI)
   ├── run job   ── background thread -> pipeline.run -> snapshot.save
   └── live data ── pipeline.build_context + season stats/projections (http.py cache), TTL-cached in memory
```

### 3.1 Snapshot (`ffa/snapshot.py`)

`snapshot.to_dict(result, warnings)` serializes a `pipeline.Result` to JSON. It's saved as
`reports/latest_run.json`. Each run is also archived to `reports/archive/<YYYY-MM-DD_HHMMSS>_weekNN_YYYY/`
(local time; `-2`, `-3`... if two runs share a second), which holds `run.json` plus copies of that run's Markdown
report and CSV. Archived runs are never overwritten. The folder name comes from `archive_dir` in `config.json`. Contents:

| Key | Contents |
|---|---|
| `meta` | team, league name/id, season, week, `generated_at` (ISO, UTC), run options (news, weather), roster slots, FAAB budget left |
| `lineup` | `[{slot, player_id}]` in roster-slot order |
| `current_starters` | Sleeper's current starters |
| `players` | `player_id -> PlayerEval` dict for roster, reserve and every evaluated free agent, plus `group` (`roster`/`reserve`/`candidate`) |
| `waiver_recs` | `[{player_id, weekly_gain, ros_gain, drop_id, bid, score}]` |
| `changes` | `{start: [ids], bench: [ids]}` vs. current Sleeper lineup |
| `alerts` | roster ids with a bye or injury multiplier < 1 |
| `total` | projected adjusted lineup total |
| `warnings` | `[warn]` lines printed during the run |

`snapshot.capture()` is a context manager that tees stdout and collects the `[warn]` lines. The CLI and
the UI's run job both use it, so warnings appear in the snapshot no matter where the run started.

News detail: `Sentiment` gains an `articles` list (title, score, source, link, published), stored
on `PlayerEval.articles`. The existing `headlines` field (top 3) keeps the Markdown report unchanged.

### 3.2 Live data

`pipeline.build_context(...)` is the fetch half of `pipeline.run`, split out so the server can build a
`Context` without evaluating anything. `pipeline.run` calls it, so nothing changes for the CLI. Context
gains `users` and `rosters` (for "who owns this player").

The server's `Live` object holds a context and an `Evaluator`, plus per-week stats and projections for
weeks `1..week` (fetched on demand through `sleeper.stats/projections`, which are already cached on
disk for an hour). It's rebuilt after `ui.live_ttl_minutes` or when a run finishes. Live evaluations
have **no sentiment**, because scraping news for hundreds of players is too slow. The UI labels them
so, and uses the snapshot's evaluation whenever the player was in the last run.

### 3.3 Background run

`POST /api/run` starts `pipeline.run` in a thread. Only one run at a time is allowed: a second POST
returns 409. `GET /api/run` returns `{state: idle|running|done|error, started_at, finished_at, log, error}`,
where `log` is the captured progress lines. The frontend polls it every 1.5 s while running, shows the
latest log line, and reloads the current tab when the run finishes.

### 3.4 API

| Method & path | Returns |
|---|---|
| `GET /api/summary?run=` | the latest snapshot, or the archived run `run` (404 `{detail}` if none) + `stale` flag + `current_week` + `archived` |
| `POST /api/run` `{news, weather}` | `202` job status, `409` if running |
| `GET /api/run` | job status |
| `GET /api/players/search?q=&limit=` | `[{player_id, name, position, team, owner}]`, fantasy positions only, prefix matches first |
| `GET /api/players/{id}` | profile, owner, `history` per week (opp, actual, projected, key stats), this week's eval (`source: snapshot|live`), snapshot articles |
| `GET /api/waivers/pool` | every free agent at a fantasy position with a team: live eval + season points/games + trending adds |
| `GET /api/waivers/whatif/{id}` | `{weekly_gain, ros_gain, drop, bid}` for adding that player (same math as `waivers.recommend`, no thresholds) |
| `GET /api/news?run=` | scored players from the latest (or archived) snapshot with sentiment and articles |
| `GET /api/runs` | archived runs, newest first: id, `generated_at`, week, total, options, counts; `latest` flags the current one |
| `GET /api/compare?run=&to=` | `snapshot.compare` of archived run `run` against `to` (default: latest): total delta, per-player adj/slot changes, added/dropped players, waiver targets new/gone/kept |
| `GET /api/news/{id}` | live news + sentiment for any player (Google News + generic feeds) |

`waivers.evaluate_add(ctx, my_evals, starters, cand)` is pulled out of `waivers.recommend` so
`whatif` and `recommend` share the math.

## 4. Pages and UX

Global shell: a top bar with the team name, `League · Week N`, a snapshot-age chip (amber when stale:
older than `ui.stale_hours` or from an earlier week), and the **Run pipeline** button. The button has a
small menu with *News* and *Weather* checkboxes. Below the bar are the tabs, in order:
Summary · Roster · Player · Waivers · News. Routing uses the URL hash (`#roster`, `#player/4034`), so
the back button and bookmarks work. Every player name anywhere links to `#player/{id}`.

Shared visual language:
- **Adjusted points** are the headline number in bold, always with the same formatting.
- **Multiplier chips**: ×0.85 injury, ×1.07 matchup, etc. Green above 1, red below 1, hidden when exactly 1.
- **Injury chip** colors: Q amber, D/O/IR red.
- **Matchup grade** A–F pill.
- **Sentiment bar**: a centered −1..+1 bar.
- Light and dark themes follow the OS.

Empty, loading and error states: every page has a skeleton while loading. With no snapshot, Summary shows
"No runs yet" with a primary Run button, and Roster/News point back to it. Live-data failures show an
inline error banner, never a blank page.

### 4.1 Summary (home)

Goal: everything needed on Sunday morning in one screen.
1. **Tiles**: Projected total (adjusted) · Lineup changes needed · Alerts (injury/bye) · Top waiver target
   (name, gain, bid).
2. **Before kickoff** checklist, in priority order:
   - START/BENCH moves vs. the current Sleeper lineup
   - Empty or zero-point slots ("No healthy RB")
   - Injury/bye alerts on the roster
   - Top 3 waiver adds, each with its drop and bid
3. **Recommended lineup**, compact: slot, player, opponent, adjusted points, chips.
4. **Run warnings**, if any, in a collapsible panel.

### 4.2 Roster

- Three sections: **Starters** (by recommended slot), **Bench**, **IR/Taxi**.
- **Columns:** Player, Pos, Opp, Proj, Recent, Matchup, Weather, Sentiment, Injury, **Adj**.
- **Lineup diff markers:** a row in the recommended lineup but not in Sleeper's lineup gets a green "START"
  tag. A Sleeper starter the model would bench gets an amber "BENCH" tag.
- Clicking a row expands a **"Why this number"** panel with the waterfall
  `base (0.7×proj + 0.3×recent) → ×injury → ×matchup → ×weather → ×sentiment = adj`. The panel also shows
  the defense rank/ratio, the implied team total, the weather summary and the notes, plus an "Open player page" link.

### 4.3 Player

- A search box with type-ahead (debounced 200 ms) across all NFL players at fantasy positions. Results show
  name, position/team and an owner chip (Mine / team name / FA). With no player selected, the page shows
  quick picks: your roster.
- **Header**: name, position, team, injury chip, owner chip, trending adds.
- **Chart (inline SVG)**: bars = actual points per week this season, line = Sleeper's projection for each
  week, outlined bar = this week's projection. Bye weeks are labeled, and DNP is shown as a gap.
- **This week**: opponent, adjusted points, multiplier chips and the "why" waterfall (same component as Roster).
  A "live estimate, no news" label appears when the evaluation isn't from the snapshot.
- **Game log** table: Week, Opp, Actual, Proj, Diff (colored), plus key stats (pass/rush/rec yards and TDs, receptions).
- **News** for this player from the snapshot, and a "Fetch latest news" button that calls `/api/news/{id}`.

### 4.4 Waivers

A segmented control switches between **Recommended** and **Browse all**.
- **Recommended**: the ranked `waiver_recs` as cards/rows: player, opponent, adj, lineup gain (wk),
  value vs. drop, adds (48h), suggested drop, bid. Empty state: "No free agents clear the thresholds."
- **Browse all**: the full pool from `/api/waivers/pool`.
  - Controls: name search, position chips (QB RB WR TE K DEF), team select, "hide injured", "only trending".
  - Sortable columns: Proj, Recent, Adj, Season pts, Pts/game, Adds (48h). The default sort is Adj, descending.
  - Paged client-side (50 rows) with a count ("312 players").
  - Each row has a **"What if?"** button that calls `/api/waivers/whatif/{id}` and shows the lineup gain,
    value vs. drop, drop and bid inline.

### 4.5 News

- Filter chips: **My roster · Waiver targets · All scored**. A sort toggle switches between strongest
  signal (|sentiment|) and most articles.
- Each player row shows name, position/team, the sentiment bar with its value, article count and the
  resulting multiplier.
- Expanding a row lists the articles: score chip, title (link opens a new tab), source and relative date.
- A "Look up any player" search runs a live fetch for a player who isn't in the snapshot.
- A footnote explains how the sentiment score becomes the multiplier (`1 + sentiment_weight × score`).

### 4.6 Archived runs

- A **run picker** in the top bar (hidden until a second run exists) lists "Latest run" and every archived run
  as `Sun, Oct 4, 1:52 PM · Wk 5 · 93.2` (plus "no news" when news was skipped).
- Picking one adds `?run=<id>` to the hash (`#roster?run=…`). Tab links and player links keep it, so the choice
  survives navigation, bookmarks and the back button.
- Summary, Roster, Waivers → Recommended and News render the archived snapshot under an "archived run" banner
  with a **Back to latest** link. The Player page, Browse all and What if? stay live and say so.
- Summary adds a **Compared with latest** card: the change in lineup total (flagged when the weeks differ),
  a table of players whose adjusted points, slot or roster status changed (with an "N unchanged" count), and
  waiver targets that are new, no longer recommended, or still recommended.

## 5. Configuration

New `config.json` block (no constants hardcoded in modules):

```json
"ui": {"host": "127.0.0.1", "port": 8000, "live_ttl_minutes": 15, "stale_hours": 24,
       "search_limit": 15, "pool_page_size": 50}
```

## 6. CLI

```bash
python -m ffa ui --username USER [--port 8000] [--week N] [--out reports]
```

`python -m ffa` (the weekly report) now also writes the snapshot.

## 7. Testing

- `tests/conftest.py` holds the `fake_world` fixture, shared by the existing tests and the new server tests.
- `tests/test_server.py` uses FastAPI's `TestClient` (with `httpx`), entirely offline:
  - summary 404 before a run, then the run job end to end (thread joined), then summary contents
  - search ranking and owner labels
  - player detail history (actual vs. projected, bye handling) and eval source
  - waiver pool excludes rostered players; what-if matches `waivers.recommend` for the same player
  - news list from the snapshot; live news through the patched `NewsScorer`
  - the static index is served at `/`
- Snapshot round trip: `to_dict` output is JSON-serializable and has the keys the frontend uses.

## 8. File layout

```
ffa/
  server.py      # FastAPI app, run job, live data
  snapshot.py    # Result -> JSON, save/load, stdout capture
  web/
    index.html
    app.js
    style.css
docs/ui-design.md
tests/conftest.py
tests/test_server.py
```

## 9. Open questions / later

- Phone access: a hosted mode would need auth and a deploy target. Out of scope for now.
- Once the eval-loop store exists, the snapshot could be read from it, and a "History" tab could compare past
  predictions with actuals.
- League-wide views (other teams' rosters, trade analysis) and the transaction ledger could become extra tabs.
