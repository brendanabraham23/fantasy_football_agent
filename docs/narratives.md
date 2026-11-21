# Narrative backtest

Question: do the expert calls inside news articles ("start him", "smash spot", "fade") predict when a player beats
his projection? Headline sentiment can't answer this. Most of these calls are in the article body, and multi-player
headlines ("Start/Sit: A, B, C") carry no verdict. Rescoring weeks 6-10 headlines with the LLM, all of them or
forward-looking ones only, found no signal (best-fit weight about -0.8, 95% CI about -1.9 to +0.4, n=126).

## What it does (`ffa/narratives.py`, `python -m ffa narratives`)

1. **Rows.** For each player-week in `--weeks`, take the newest archived run made before that player's kickoff.
   Keep the articles it saw that were published before kickoff, skipping player hub pages by title
   (`narratives.skip_titles`), since those update continuously. `pre` = the run's `adj` with the sentiment
   multiplier divided out.
2. **Fetch.** Resolve Google News links to the publisher URL (Google's `batchexecute` endpoint), fetch the page
   and extract the text with trafilatura. Everything is cached through `http.py` for `narratives.cache_days`.
3. **Leakage guard.** Drop an article if its page metadata (`dateModified`, `article:modified_time`,
   `og:updated_time`) says it was changed at or after kickoff. Pages that declare no modified time can't be
   checked. That's a known limitation, so look at `calls.csv` quotes for anything that reads like a result.
4. **Read.** Keep the paragraphs naming the player plus the paragraph after each one (start/sit pieces put the
   call under a name heading), capped at `excerpt_chars`. One Claude call per player-week returns, for each
   article: `kind` (call / outlook / recap / none), `lean` in [-1, 1] *relative to the player's usual
   expectations*, the week it's about, and a short supporting quote. Results are cached in
   `reports/narratives/llm_cache.json`.
5. **Test.** `narrative` = mean lean of this week's `call` articles. Same model as the sentiment study:
   `adj = pre x (1 + w x lean)`, with w fit by least squares, a 95% CI from a player-level bootstrap, and
   leave-one-week-out CV MAE against `w = 0`. Also reported: hit rates (share of players who beat `pre` when the
   lean is above +0.15 vs below -0.15), and headline sentiment on the same rows for comparison.
   Actuals use league scoring from Sleeper.

## Outputs (`reports/narratives/`)

`narratives.md` (coverage funnel and results), `rows.csv` (one row per player-week), `calls.csv` (one row per
article read: kind, lean, quote, URL), `fetch_errors.json` (failures by type), `llm_cache.json`.
Article text is never written to disk outside the HTTP cache.

## Running

- GitHub Actions: `narratives backtest` (`.github/workflows/narratives.yml`), manual dispatch with an optional
  `weeks` input. It needs `SLEEPER_USERNAME` and the `CLAUDE_CODE_OAUTH_TOKEN` secret, and commits
  `reports/narratives/` to `data`. The workflow file must be on the default branch before it can be dispatched.
- Locally: `python -m ffa narratives --username USER --archive <data checkout>/reports/archive --weeks 6-10`.

Cost: about 1,750 unique links for weeks 6-10, so roughly 5k HTTP requests (rate-limited by `delay` and
`fetch_workers`), plus one Sonnet call per player-week (~130).

## Reading the result

A useful signal shows up as all of these together: positive `w` with a CI clear of 0, CV MAE below the
`w = 0` MAE, and a higher beat rate for positive leans than for negative ones. With ~130 player-weeks only a
large effect will clear the bar. If the result is promising but not conclusive, the next step is to record calls
live each week (fetch + read inside the pipeline, stored alongside predictions) with weight 0, and re-test.
