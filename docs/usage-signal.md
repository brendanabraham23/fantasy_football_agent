# Usage signal: recent form from targets and carries

Recent form used to be the average of a player's fantasy points over the last `recent_weeks` games. For RB, WR and
TE it is now mostly **expected points from volume**:

```
usage_pts   = pts_per_target[pos] x targets/game + pts_per_carry[pos] x carries/game     (last recent_weeks games)
recent_form = (1 - usage.weight) x recent_avg + usage.weight x usage_pts
base        = weights.projection x proj + weights.recent_form x recent_form
```

`pts_per_target` and `pts_per_carry` are fit each run by least squares over every player's games in the same
weeks, in the league's scoring (`usage.coefficients`). A position with fewer than `usage.min_games` games, or
no recorded targets and carries, keeps points-only form. QB, K and DEF are unchanged. `usage.enabled: false`
restores the old behavior.

## Why

Week 9, 2026: Denzel Boston had scored 2.5, 5.7 and 9.7 but drew 8 targets a game, and the model ranked him
behind two receivers he outscored by 9. Volume is steadier than points, which swing on touchdowns and long plays.

## Backtest (2026 weeks 4-9, run 2026-11-10)

Predicting a player's next game from his previous three (nflverse weekly data, PPR, coefficients fit only on
earlier weeks):

| Recent-form signal | Player-weeks | MAE | Correlation |
|---|---|---|---|
| Points average (old) | 1,445 | 4.63 | 0.575 |
| Usage expected points | 1,445 | 4.40 | 0.615 |
| 25% points + 75% usage (shipped) | 1,445 | 4.39 | 0.616 |

The MAE gain of 0.23 has a 90% bootstrap interval of 0.12 to 0.34. It holds for each position (TE most, 4.45 to
4.06) and among players averaging 5+ points (6.34 to 5.91).

Inside the full blend the effect is small, because recent form is 30% of `base`. On the 136 player-weeks where a
Sleeper projection was available (33 rostered or waiver-pool players, weeks 4-8), MAE went from 5.71 to 5.64;
the 90% interval on that gain, -0.06 to 0.23, includes zero.

Things tried that did not help: weighting the most recent game more heavily (0.5/0.3/0.2) was slightly worse
than a flat average for both points and usage.

## Replaying Week 9

| Player | Old adj | New adj | Actual |
|---|---|---|---|
| Denzel Boston | 8.4 | 9.8 | 15.8 |
| Jalen Coker | 11.1 | 11.7 | 6.6 |
| Keenan Allen | 12.5 | 12.4 | 6.6 |
| Ashton Jeanty | 13.1 | 12.6 | 7.0 |

The numbers move the right way for Boston and Jeanty, but the recommended lineup would not have changed.

## Checking it with Sleeper's projections

`python -m ffa calibrate --username USER --weeks 4-9` now reports `usage` next to `recent`, and `base` (with
usage) next to `base_points` (without), on the same rows. That run covers every projected player, not just the
33 above, and also reports the best projection weight for the blend.
