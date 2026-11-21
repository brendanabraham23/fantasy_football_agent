# Narrative backtest: 2026 weeks 6-10

Does an article's call on a player (read from the article body) predict whether he beats the run's prediction with sentiment removed? Model: `adj = pre x (1 + w x lean)`; positive `w` means the calls help.

## Coverage

| step | count |
|---|---|
| player-weeks | 126 |
| articles (pre-kickoff, not hub pages) | 1802 |
| unique links | 1755 |
| fetched | 1364 |
| edited after kickoff (dropped) | 166 |
| with paragraphs naming the player | 1159 |
| player-weeks the LLM couldn't read | 0 |
| calls about this week | 409 |
| player-weeks with at least one call | 114 |

## Results

| signal | rows | players | Spearman (p) | best w (95% CI) | P(w>0) | MAE w=0 | CV MAE | beat pre: lean>+.15 / lean<-.15 / all |
|---|---|---|---|---|---|---|---|---|
| narrative (calls) | 114 | 50 | -0.121 (0.20) | -0.34 [-0.71, -0.01] | 0.02 | 6.412 | 6.476 | 39% (n=51) / 41% (n=29) / 37% |
| headline sentiment, same rows | 114 | 50 | -0.003 (0.98) | -0.71 [-1.62, +0.19] | 0.07 | 6.412 | 6.466 | 39% (n=18) / 0% (n=4) / 37% |
| narrative+outlook | 124 | 51 | -0.162 (0.07) | -0.56 [-1.01, -0.10] | 0.01 | 6.331 | 6.298 | 33% (n=45) / 46% (n=28) / 39% |
| narrative, all rows (0 if no call) | 126 | 51 | -0.137 (0.13) | -0.34 [-0.70, +0.01] | 0.03 | 6.278 | 6.336 | 39% (n=51) / 41% (n=29) / 39% |

MAE by weight:

| signal | w=0 | w=0.04 | w=0.08 | w=0.15 | w=0.25 | w=0.5 |
|---|---|---|---|---|---|---|
| narrative (calls) | 6.412 | 6.420 | 6.435 | 6.480 | 6.568 | 6.930 |
| headline sentiment, same rows | 6.412 | 6.417 | 6.422 | 6.431 | 6.450 | 6.517 |
| narrative+outlook | 6.331 | 6.344 | 6.363 | 6.406 | 6.486 | 6.722 |
| narrative, all rows (0 if no call) | 6.278 | 6.285 | 6.299 | 6.339 | 6.419 | 6.747 |

Notes:
- `narrative` = mean lean of `call` articles about this week; `narrative+outlook` also counts `outlook`; `headline sentiment` is what the run used, on the same rows.
- Articles whose page says it was modified after kickoff are dropped (pages that don't declare a modified time can't be checked). Player hub pages are skipped by title (`narratives.skip_titles`).
- CV MAE fits w on the other weeks and scores the held-out week; compare it with MAE w=0.
- Leans with |lean| <= 0.0 are ignored.
