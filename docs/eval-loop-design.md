# Design: Prediction Logging, Backtesting, and Weekly Evaluation

**Status:** Draft. The transaction ledger (§4.5) is implemented in `ffa/ledger.py`. · **Author:** Brendan · **Created:** 2026-10-02

## 1. Summary

The analyzer produces a weekly adjusted point estimate for each player, but nothing records whether those estimates were right. This spec adds a closed feedback loop with five parts:

1. Every prediction run is logged to a local store, with the raw inputs behind each estimate.
2. Every Tuesday, predictions are scored against actual results.
3. Large misses are attributed to error types by position.
4. Config changes are proposed, and each one is backtested before it can be adopted.
5. The same machinery replays past seasons, so the model can be tuned on 2025 now rather than after weeks of live data.

The goal is to win more matchups, not to minimize point error for its own sake. Decision quality is therefore the headline metric. It covers both **roster decisions** (waiver claims, free-agent adds, drops, trades) and **lineup decisions** (who starts). It is measured against what was actually available at each decision point, not just against the roster I ended up with.

## 2. Goals and non-goals

**Goals**
- Measure, per position, whether each adjustment (recent form, matchup, Vegas, weather, injury, sentiment) makes predictions better or worse than the raw Sleeper projection.
- Measure decision quality at both levels. Roster decisions are judged against the free-agent pool that was actually available. Lineup decisions are judged against my final roster.
- Tell apart a bad model, a model whose recommendations I didn't follow, and bad luck.
- Learn which signals before the week identify good pickups, so the waiver logic improves over the season.
- Separate errors we can fix (role changes, signals that were wrong) from noise we should not tune on (touchdown variance).
- Let one command backtest any config against any set of past weeks.
- Run unattended on a schedule.

**Non-goals (for now)**
- Replacing Sleeper projections with an in-house model.
- Changing the config automatically. All changes go through a PR.
- Backtesting news sentiment. Historical Google News results can't be reconstructed reliably, so sentiment is evaluated only on live data.

## 3. Background: gaps in the current system

| Gap | Consequence |
|---|---|
| The CSV logs only my roster and waiver candidates | The sample is too small and biased to evaluate on |
| Each multiplier is stored only after clipping, and DvP and Vegas are combined into one clipped value | Can't tell how much each signal contributes |
| Inputs and scoring are mixed together in `Evaluator.evaluate` | Re-scoring under a new config means fetching all the data again |
| `date.today()`, the live Sleeper injury status, and current team assignments are read directly | Past weeks can't be replayed without leaking future information |
| Nothing pulls actual results | No feedback signal |

One finding motivates this work. On 2025 nflverse data, points allowed by each defense in odd weeks barely predict points allowed in even weeks. The correlations are RB 0.08, WR 0.22, TE 0.27, and QB 0.30. Even so, the matchup multiplier can currently move a projection by up to ±20%, and Sleeper's projections likely already account for the opponent. Without evaluation there is no way to catch miscalibration like this.

## 4. Design

### 4.1 Separate inputs from scoring

Split `ranker.Evaluator` into two steps:

- **`extract_features(pid, ctx) -> Features`** gathers raw inputs only and applies no config: projection, recent average, recent usage, the defense ratio and how many games it's based on, implied team total and the week's average, opponent implied total, weather fields, injury status, sentiment score and article count, bye flag, and kickoff time.
- **`score(features, cfg) -> Scored`** is a pure function. It computes the base value and each multiplier before clipping (`m_dvp`, `m_vegas`, `m_matchup_clipped`, `m_weather`, `m_injury`, `m_sentiment`) and the adjusted value.

`PlayerEval` remains the output used by the report, built from `Features` plus `Scored`. Because `score` is pure, tuning re-scores logged inputs in memory with no network calls. This is the most important refactor in the spec.

### 4.2 An "as-of" context for replaying past weeks

Add an `AsOf` object passed through the pipeline in place of implicit "now".

| Input | Live mode | Backtest mode |
|---|---|---|
| Current date and time | system clock | kickoff of the target week minus a configurable lead time (default Sunday 11:45 ET) |
| Projections | Sleeper `projections/{season}/{week}` | same endpoint (see open question Q1) |
| Recent form | Sleeper stats for prior weeks | same, limited to weeks before the target week |
| Injury status | Sleeper player data | nflverse `injuries_{season}.csv`, final game status for that week |
| Player's team | Sleeper player data | nflverse `roster_weekly_{season}.csv` |
| Defense vs. position | nflverse, weeks before the target week | same (already free of leakage) |
| Vegas lines | nflverse schedule | same (closing lines; see section 8) |
| Weather | Open-Meteo forecast | Open-Meteo historical forecast API |
| News sentiment | Google News and RSS | disabled (sentiment is set to 0) |

### 4.3 Prediction log

Use a single DuckDB file at `data/ffa.duckdb`, with Parquet export for portability. It holds three tables.

**`runs`**: `run_id` (uuid), `run_ts` (UTC), `mode` (`live` or `backtest`), `season`, `week`, `as_of_ts`, `git_sha`, `config_hash`, `config_json`.

**`predictions`**, one row per player per run:
- Keys: `run_id`, `season`, `week`, `sleeper_id`, `gsis_id`, `position`, `team`, `opp`, `kickoff_ts`.
- Features: everything from §4.1.
- Score: `base`, `m_dvp`, `m_vegas`, `m_matchup`, `m_weather`, `m_injury`, `m_sentiment`, `adj`.
- Context: `on_my_roster`, `rostered_in_league`, `rostered_by` (roster_id), `slot_recommended`, `in_lineup_set_in_sleeper`, `in_candidate_pool` (whether waiver logic considered him), and `waiver_rank` and `waiver_score` if he was considered.

**Who gets logged:** every player with a Sleeper projection of at least 1.0 point (configurable), every rostered player in the league, and every free agent with any projection or trending activity. That's about 450–600 rows per run. Logging the full free-agent pool is required for §4.5. The pool can't be reconstructed reliably afterward.

**`recommendations`**, one row per recommended move per run: `run_id`, `type` (`add`, `drop`, `start`, `bench`), `sleeper_id`, `paired_drop_id`, `suggested_bid`, `weekly_gain`, `ros_gain`, `score`. This records what the model advised, which may differ from what I did.

**`transactions`**, pulled from Sleeper `league/{id}/transactions/{week}` for **every team**: `transaction_id`, `ts`, `type` (`waiver`, `free_agent`, `trade`), `status` (`complete`, `failed`), `roster_id`, `adds`, `drops`, `waiver_bid`. Failed claims count. They show who was outbid and by how much.

**`roster_snapshots`**: each team's roster at three decision points per week. These are the **waiver deadline** (just before Sleeper processes claims), **after waivers** (start of free agency), and **lock** (each player's kickoff). Snapshots are built from logged runs plus transactions, and they define what was available when.

**`actuals`**, one row per player per week: `fantasy_pts` (league scoring), `played`, `offense_snaps`, `offense_pct`, `targets`, `carries`, `pass_att`, `receptions`, `td_pts` (points from touchdowns), `team_pts`, `opp_pts`, observed weather, `source_ts`, and `corrected` (true if re-scored after stat corrections).

**The prediction that counts:** for each player and week, the evaluated prediction is the last live run whose `run_ts` falls before that player's `kickoff_ts`. This makes multiple runs per week safe. A Thursday player is judged on the Thursday run, and a Sunday player on the Sunday-morning run.

**Player ID mapping:** use DynastyProcess `db_playerids.csv` (`sleeper_id` ↔ `gsis_id` ↔ `pfr_id`), cached daily. nflverse stats use `gsis_id`, and snap counts use `pfr_player_id`. Unmatched IDs are logged and counted in the evaluation report.

### 4.4 Actuals ingestion (`ffa/actuals.py`)

- Fantasy points come from Sleeper `stats/{season}/{week}`, scored with the existing `sleeper.fantasy_points` and the league's scoring settings. This keeps evaluation on exactly the same scale as the predictions.
- Usage comes from nflverse `stats_player_week_{season}.csv` (targets, carries, attempts, touchdowns) and `snap_counts_{season}.csv` (offense snap share).
- Game results come from nflverse `games.csv` (scores).
- Observed weather comes from the Open-Meteo archive at the same stadium and kickoff window used for the forecast.
- If nflverse hasn't published the week by Tuesday, the job evaluates points only, marks attribution as `pending`, and a Wednesday retry fills it in.
- **Stat corrections:** a Friday job re-pulls Sleeper stats for the previous week, sets `corrected=true`, and regenerates that week's evaluation if any player moved more than 0.5 points.

### 4.5 Evaluation metrics (`ffa/evaluate.py`)

All metrics are computed per position and overall, for the current week and for the season to date.

**Prediction accuracy**
- Bias (mean of predicted minus actual), mean absolute error, and RMSE.
- Spearman rank correlation within each position and week. This matters most because start/sit decisions are rankings.
- Calibration: actual average compared with predicted average in five buckets of prediction size.

**Contribution of each adjustment (ablation).** Because `score()` is pure, recompute `adj` with each adjustment turned off, one at a time:

| Variant | How it's computed |
|---|---|
| `raw` | Sleeper projection only |
| `base` | the projection and recent-form blend, with no multipliers |
| `-dvp`, `-vegas`, `-weather`, `-injury`, `-sentiment` | full model with that one multiplier set to 1.0 (matchup clipping recomputed) |
| `full` | `adj` as logged |

The reported "lift" for each adjustment is MAE without it minus MAE with it. A positive lift means it helps. Show lift per position along with the number of players, and flag any lift whose 90% bootstrap confidence interval (resampled by player-week) includes zero.

**Decision quality (headline metric)**

A week has two decision stages, and each is judged against what was achievable at that stage.

| Stage | Decision | Choices available | Point of comparison |
|---|---|---|---|
| Roster | waiver claims, free-agent adds, drops, trades | my roster plus the free agents available at that point (§4.3 snapshots) | best roster I could have built under the constraints below |
| Lineup | who starts in each slot | my final roster at lock | best lineup from that roster |

**Constraints on what was achievable.** A hindsight ceiling built from the whole free-agent pool is unreachable and mostly measures luck, so it is limited to moves I could actually have made:
- **Availability:** a player counts only if he was unrostered at the decision point. On waivers, a player claimed by another team counts only if my remaining budget beat the winning bid (and, for priority-order waivers, my priority was higher). Failed claims in `transactions` supply the winning bids.
- **Move budget:** at most `k` adds, where `k` is the larger of the adds I actually made and `eval.max_adds` (default 2). Each add needs a drop from my bench, and I can't drop players I've marked keep-only (config).
- **Roster rules:** roster size and position limits from the league settings.
- **Search:** with `k ≤ 2`, try every swap of the top 30 available players per position against my bench, then run the exact lineup optimizer for each combination. This is a few thousand optimizer calls, which takes seconds.

**How total regret breaks down.** All values are actual points:

```
best achievable (pool)   = best lineup after the best feasible adds/drops from the available pool
best roster (hindsight)  = best lineup from my final roster
followed recommendations = lineup I'd have had by making the recommended adds and starting the recommended lineup
my actual score          = points from the lineup I set

total regret       = best achievable (pool) − my actual score
  roster regret    = best achievable (pool) − best roster (hindsight)
  lineup regret    = best roster (hindsight) − my actual score
execution gap      = followed recommendations − my actual score
```

The execution gap separates "the model was wrong" from "the model was right and I didn't follow it." It can be negative, which means my overrides helped. That's worth knowing too.

**Judging decisions rather than outcomes.** A single week's hindsight-best free agent is often whoever happened to score twice. Three measures are less sensitive to luck:
- **Percentile within the available pool:** for each add (mine, recommended, and the alternatives), where the player's actual points fell among available players at his position that week.
- **Our ranking of the top available players:** for the 3 highest-scoring available players per position, where they ranked in our predictions *before* the week. If we consistently ranked them in our top 10, the waiver logic is failing to act. If we ranked them 40th or lower, the issue is prediction or luck (see §4.6).
- **Expected regret:** the same breakdown calculated with *predicted* points instead of actual. This shows whether the decision was right given what we knew.

**Pickups pay off over several weeks** *(implemented: `ffa/ledger.py`, `python -m ffa ledger`)*. Judging an add on the week it was made undervalues stashes and handcuffs. Each completed transaction gets an ongoing ledger entry:
- **Points added:** each week, actual points of my best lineup with the move made, minus actual points of my best lineup with the move undone (keeping the dropped player). This keeps updating while the added player stays on my roster, and I can view it over 1 week, 3 weeks, and the rest of the season.
- **FAAB efficiency:** points added per dollar, and my bid compared with the next-highest bid. Overpaying by more than 2 times the runner-up across several claims is flagged.
- **Trades** use the same ledger, comparing both sides of the trade.

**League-wide view.** The same ledger is run for every team's transactions. Other managers' best pickups become training examples: what signals did those players show beforehand that our pool or scoring missed? League-wide lineup regret is also computed and gives about 10–12 times more lineup decisions per week.

**Lineup-only detail.** Start/sit flips: decisions where the recommended lineup differed from the raw-projection lineup, with how many points each flip gained or lost.

**Report layout.** The weekly report leads with total regret broken into roster regret, lineup regret, and execution gap, for the week and season to date. A league-wide pickup leaderboard and the ledger of my own transactions follow.

### 4.6 Error attribution (`ffa/attribution.py`)

Tag each large miss with deterministic rules. A large miss is one in the top 20% of absolute error for its position that week, and at least 4 points. Rules are checked in order and the first match wins.

| Tag | Rule (thresholds configurable) | Fixable? |
|---|---|---|
| `INACTIVE` | predicted > 0, and 0 offense snaps or no stat line | Partly (check inactives, late-game pivots) |
| `EARLY_EXIT` | offense snap share < 50% of his 3-week average, and he played | No (in-game injury) |
| `ROLE_UP` / `ROLE_DOWN` | opportunity (targets + carries; pass attempts for QB) differs from his 3-week average by more than 40% | Yes (usage and news signals) |
| `GAME_SCRIPT` | game total differs from the Vegas total by more than 14, or margin differs from the spread by more than 14 | Partly |
| `WEATHER_MISS` | forecast and observed wind differ by more than 10 mph, or precipitation flipped | Partly |
| `TD_VARIANCE` | none of the above, and touchdown points explain at least 60% of the error | **No (do not tune on this)** |
| `EFFICIENCY` | none of the above | Investigate |

Kickers and defenses use reduced rules: `INACTIVE`, `GAME_SCRIPT`, `WEATHER_MISS`, and `VARIANCE`.

**Acquisition misses.** For each of the top 3 available players per position (by actual points) that I didn't add, plus any recommended add that scored in the bottom quartile of the pool, assign the first matching tag:

| Tag | Rule | What it means |
|---|---|---|
| `EXECUTION` | recommended, but I didn't claim him | I didn't follow the model |
| `OUTBID` | I claimed him and lost the bid | bidding |
| `POLICY_BLOCKED` | in our predicted top 10 at his position, but not recommended (thresholds, drop logic, or not enough roster space) | waiver logic or settings |
| `NOT_IN_POOL` | never considered (`in_candidate_pool=false`) | candidate pool too narrow |
| `SIGNAL_MISSED` | ranked low by us, but at least one leading signal fired before the week: a teammate ahead of him was Out or on IR, his snap share rose for 2 weeks, he was in Sleeper's top 25 trending adds, or his projection rose more than 30% from the week before | missing feature |
| `UNPREDICTABLE` | none of the above | luck; don't tune on it |

Lead signals are calculated as of the decision point from logged data. Counts of `SIGNAL_MISSED` by signal type go to the advisor as candidate features.

**Output:** a table of share of total absolute error by tag and position, for the week and season to date, plus the five largest misses per position with their tag, inputs, and actual usage.

### 4.7 Improvement step

**a. Statistical refit (`ffa/tuning.py`).** Tunable settings for predictions: `weights.*`, `matchup_weight`, `vegas_weight`, `matchup_clip`, `sentiment_weight`, `injury_multipliers.Questionable`, `injury_multipliers.Doubtful`, the weather coefficients (which move from constants in `weather.py` into the config), and a new `dvp_shrinkage_games`. Tunable settings for waivers: `waivers.pool_per_position`, `min_weekly_gain`, `min_ros_gain`, the weights in the waiver score, and the bid formula. Waiver settings are tuned against **roster regret and the multi-week ledger**, not MAE.

- Objective: MAE on all logged player-weeks that excludes `INACTIVE`, `EARLY_EXIT`, and `TD_VARIANCE`, so the model isn't tuned on noise it can't predict. Lineup regret is reported as a secondary check.
- Method: a coarse grid followed by bounded `scipy.optimize.minimize`, scored by leave-one-week-out cross-validation.
- A proposal is adopted only if:
  - at least 4 weeks of data exist,
  - cross-validated MAE improves by at least 2%,
  - it wins in at least 60% of held-out weeks, and
  - it does not increase lineup regret or roster regret.
- Adopted values are pulled back toward the current values (blend weight `α=0.5`) to avoid swinging back and forth.

**b. LLM analyst (`ffa/advisor.py`).** Input is only the summary tables from §4.5 and §4.6 (JSON), including the regret breakdown, the transaction ledger, and acquisition-miss counts, along with the top misses and the current config. It does not receive raw rows. Output must follow this schema:

```json
{
  "narrative_md": "...",
  "config_proposals": [{"param": "matchup_weight", "current": 0.5, "proposed": 0.15, "rationale": "..."}],
  "feature_ideas": [{"title": "...", "evidence": "...", "expected_impact": "..."}]
}
```

Every config proposal, whether from the LLM or the refit, is run through the backtest before it is shown. Results include cross-validated MAE, regret, and how many weeks it won. Feature ideas are never applied automatically. They are written up as GitHub issues.

**c. Delivery.** A PR changes `config.json` and attaches `reports/eval/weekNN_YYYY_eval.md`. Merging the PR is the only way the config changes.

### 4.8 Backtest harness

```bash
python -m ffa backtest --season 2025 --weeks 1-18 [--config path] [--league-id ...]
python -m ffa backtest --season 2025 --compare config.json config.candidate.json
```

- Builds `AsOf` contexts for each week and writes `mode=backtest` runs to the same store.
- League scoring and roster slots come from the current league settings. Backtests evaluate the model, not historical rosters, so lineup regret in backtests uses league-wide rosters only where available and otherwise is skipped.
- After the first full fetch, backtests should finish in under a minute, because tuning re-scores stored inputs and does not fetch anything.

### 4.9 Command-line interface

```bash
python -m ffa run        # existing weekly report; now also logs to the store
python -m ffa evaluate   --season 2026 --week 5   # pull actuals, score, attribute, write report
python -m ffa tune       --season 2026 [--include-backtest 2025]
python -m ffa advise     --season 2026 --week 5   # LLM narrative + proposals (requires API key)
python -m ffa backtest   ...
```

### 4.10 Scheduling (GitHub Actions)

| Workflow | Cron (UTC) | Purpose |
|---|---|---|
| `predict.yml` | Thu 22:30; Sun 15:45 and 16:45; Sun 19:30; Mon 22:00 | Live runs before each kickoff window. Two Sunday-morning entries cover the switch from EDT to EST and GitHub's cron delays. The "last run before kickoff" rule handles overlap. |
| `evaluate.yml` | Tue 12:00 | `evaluate`, then `tune`, then `advise`, then open a PR |
| `evaluate-retry.yml` | Wed 12:00 | Fill in pending attribution |
| `corrections.yml` | Fri 12:00 | Re-score after stat corrections |

Inactives are announced about 90 minutes before kickoff (11:30 ET for 1 p.m. games). 16:45 UTC is 12:45 EDT or 11:45 EST, so it lands after inactives and before kickoff all season.

**Persistence:** the job commits `data/ffa.duckdb` (well under 50 MB per season) and the reports to a dedicated `data` branch, which keeps `main` history clean. **Secrets:** an LLM API key, and the Sleeper username and league ID as repository variables.

### 4.11 Module layout

```
ffa/
  asof.py         # AsOf context + clock injection
  features.py     # extract_features (moved from ranker)
  ranker.py       # score() pure function + lineup optimizer
  store.py        # DuckDB schema, writes, "last run before kickoff" query
  ids.py          # DynastyProcess ID crosswalk
  actuals.py      # Sleeper stats + nflverse usage/snaps/games + observed weather
  evaluate.py     # metrics, ablation, regret
  attribution.py  # error tags
  tuning.py       # refit + CV + adoption rules
  advisor.py      # LLM narrative/proposals
  backtest.py     # season replay
docs/
  eval-loop-design.md
```

## 5. Testing

- Unit tests for `score()` against the current `Evaluator` output on the existing fixtures. Results must match exactly, proving the refactor changes no behavior.
- Leakage tests: in backtest mode, assert that no input carries a timestamp or week at or after the target week's kickoff.
- A fixture week with hand-built actuals that covers every attribution tag.
- A test for the "last run before kickoff" query across overlapping Thursday and Sunday runs.
- Tests for the tuning adoption rules on synthetic data where the true parameter is known.

## 6. Rollout

| Phase | Scope | Why this order |
|---|---|---|
| 1 | §4.1 refactor, §4.3 store (including the full free-agent pool, recommendations, transactions, and snapshots), logging in `run`, `predict.yml` | Start collecting live 2026 data now. Sentiment can only be evaluated live, so every week lost is lost for good. |
| 2 | §4.4 actuals, §4.5 metrics, `evaluate.yml` | The Tuesday report is useful on its own |
| 3 | §4.2 as-of context, §4.8 backtest of 2025 | Gives a full season of tuning data immediately |
| 4 | §4.6 attribution, the roster-stage regret, and the transaction ledger | Needs full-pool logging and transactions from phase 1 |
| 5 | §4.7 refit, advisor, PR flow | Needs phases 2–4 |

## 7. Open questions

- **Q1:** Does Sleeper's historical `projections/{season}/{week}` return the final pre-kickoff projection, or values updated after kickoff? Check this by comparing against Sunday-morning runs logged in phase 1. If it leaks, backtests will look better than reality.
- **Q2:** Is nflverse `injuries_2026.csv` published and kept current? It's only needed for backtests of the current season.
- **Q3:** Should league-wide regret count only lineups I can see before lock, or every team's final lineups?
- **Q4:** Which LLM to use for the advisor: the Anthropic API or Bedrock?
- **Q5:** Should the wider-scope accuracy fixes from the code review land before phase 1? These are DvP shrinkage, usage-based recent form, consensus projections, and an exact lineup optimizer. Recommendation: no. Land them after phase 3 so the backtest can measure each one.
- **Q6 (decided):** `eval.max_adds` starts at **3**. The ledger report checks it against the league's actual adds per team per week (mean, 75th and 90th percentiles, max, and the top-half teams' average) and suggests raising it if the 90th percentile exceeds it.
- **Q7:** Can roster-stage regret be backtested for 2025? Sleeper transactions for last season's league are available if the league ID is known, but free-agent pools would have to be rebuilt from transactions alone. That's probably good enough for the waiver deadline and less reliable for free agency later in the week.

## 8. Risks

- **Closing versus opening lines.** nflverse lines in backtests are close to closing lines, which are sharper than what a Sunday-morning run would have seen. The Vegas adjustment will look slightly better in backtests than live.
- **Small samples.** One week has only about 30–60 non-noise large misses per position. Adoption rules require several weeks of data and consistency across held-out weeks.
- **Google News rate limits** from GitHub Actions IP addresses. Mitigation: the existing 3-hour cache, fewer workers, and limiting news to rostered players and top waiver candidates.
- **Undocumented Sleeper endpoints** can change. Mitigation: schema checks in `actuals.py` that fail loudly.
