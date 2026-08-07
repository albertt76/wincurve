# NBA Player Impact & Wins — Methodology (for technical review)

*wincurve · one-page methods note · reviewer focus: estimator correctness and how it relates to published player-value models.*

## 1. What "Impact" is

**Impact** is a single per-player rate: **points per 100 possessions of net team-scoring value relative to a league-average player**, split into an **offensive** and **defensive** component (0 = average; ≈ −1.45 = replacement level; a star ≈ +4 to +6). It is a *talent rate*, not a win count — wins are a separate conversion (§4).

The design is **bottom-up**: there are ~10,600 player-seasons (2005–06 → 2025–26) but only 630 team-seasons, so all signal is extracted at the player level and the team layer stays nearly parameter-free. Offense and defense are modeled, aged, and calibrated **separately** throughout.

## 2. The estimator (this is the part to scrutinize)

Our Impact metric is a **box-plus-tracking linear model fit to team outcomes** — architecturally the same family as Basketball-Reference's **BPM (Box Plus/Minus)** and the box component of FiveThirtyEight's **RAPTOR**, *not* a direct play-by-play plus/minus model. Concretely, per walk-forward fold:

1. **Features**, all per-100-possessions and **z-scored within season** (the league changed enormously — pace ~90→100, three-point attempts ~16→38/game):
   - *Offense:* points, made threes, field-goal attempts, free-throw attempts, assists, turnovers, offensive rebounds, true-shooting %, three-point rate. Volume **and** efficiency both enter deliberately (ridge absorbs the collinearity).
   - *Defense:* steals, blocks, defensive rebounds, personal fouls, plus `def_rating_rel` (an on-court/off-court term), plus player-**tracking** features where available (2013–14+): rim field-goal % suppression as nearest defender, rim volume, a points-saved product, deflections, and contested twos.
2. **Positional de-confounding:** *defensive rebounding only* is standardized **within position group (guard/forward/center)** rather than league-wide, because grabbing a defensive board is mostly positional role, not skill. This removed a 26 %-weight "is this player a center" confound (centers averaged +1.03 def, guards −0.33 → now +0.09 / +0.04). Blocks/steals/rim-protection stay league-wide (genuinely cross-positional).
3. **Fit:** **ridge regression** (L2 penalty, `alpha = 25`) mapping **minute-weighted team-aggregate features → team rating deviation from league mean** — offense features → team offensive rating, defense features → (sign-flipped) team defensive rating, as two separate fits. `alpha` was chosen by **downstream projection correlation**, not in-sample fit (0.595 at α=1 → 0.698 at α=25, plateau after).
4. **Score players:** apply the fitted team-level weights to each player's own z-scored features; divide by 5 (a player fills one of five floor spots) to land on the conventional per-player per-100 scale. The intercept (league baseline) is excluded so Impact is relative to average.

Fitting the coefficients on **team aggregates against team point-differential**, then applying them to individuals, is exactly BPM's identifying assumption: the metric is anchored to what actually moved team scoring margin, not to a hand-set stat weighting.

**RAPM as a second signal.** We *also* compute **RAPM (Regularized Adjusted Plus/Minus)** from reconstructed play-by-play stints (ridge `alpha = 2000`, shrunk toward the current box Impact as a Bayesian prior — a "box-informed RAPM" like EPM/LEBRON's anchoring). RAPM is **blended into the defensive team aggregate, weighted by roster turnover** (`agg_def = (1−w)·box + w·RAPM`, w = new-minute share): a stable roster's defense is carried by the box metric + carryover, a churned roster's by RAPM (which travels with the player). At the *player* level RAPM is shown for comparison only.

**Walk-forward discipline:** every coefficient — ridge weights, aging deltas, calibration slopes, `rho` — is refit each season on **strictly prior seasons only**. Features for season N use only information available before season N tipped off.

## 3. Projecting talent forward

Each player's Impact for the target season is: **(a)** minute-weighted blend of his last 3 seasons (recency weights 5/3/2); **(b)** shrink toward an **age-mean prior**, weight-of-evidence in minutes (`shrink_minutes = 200`, so a ~300-min player is mostly prior, a 2,500-min player mostly himself); **(c)** add a **per-skill aging delta** (peak overall ≈ age 25; three-point volume peaks latest ~29, blocks decline from the start — aging is modeled per skill, not one curve).

**Team aggregation:** minute-weighted mean of projected player Impact, capped at the 240-minute/game budget; minutes freed by projected absence are priced *between* replacement level and the team's own rotation quality (measured absence-absorption ≈ 0.68, not full replacement). A **calibration slope** maps the *projected* aggregate → team rating deviation — critically fit on **projected** aggregates (which are deliberately narrower than contemporaneous ones), not same-season ones. A **one-year residual carryover** (`+ rho · last-season residual`, `rho ≈ 0.36`) captures the ~AR(1) persistence in our team-level misses (≈ 70 % defensive).

## 4. Two wins numbers, two purposes

- **Team win total** = **Monte Carlo season simulation** over the real schedule (home-court, rest, rating → expected margin → game probability), yielding a full **distribution** — mean + calibrated 80 % interval (empirical coverage 79.6 %), not a point. This is the shipped projection.
- **Per-player ≈ Wins** (the leaderboard column) = a **linear WAR-style (wins-above-replacement) decomposition**: `≈Wins = 2.38 wins/point × minute-share × slope × (player − replacement)`, priced by the same off/def slopes and RAPM-blended defense as the team rating. It is capped by the same 240-min budget factor, so per-player ≈Wins **sum exactly to the team's rating-above-replacement** (verified max error 0.000 across all 30 teams). It is a *linear attribution* of the rating, deliberately distinct from the nonlinear simulated team total.

## 5. How this compares to published models

| Model | Core method | Relationship to ours |
|---|---|---|
| **BPM** (Basketball-Reference) | Box stats fit to team outcomes | **Closest cousin.** We add off/def decoupling with separate calibrations, within-season & within-position standardization, tracking features, and a RAPM defensive blend. |
| **RAPM** | Play-by-play ridge plus/minus, no box | We compute it (box-informed) but use it as a **defensive blend + display**, not the backbone: single-season RAPM needs many possessions, and post-fixes our box metric out-predicts pure RAPM for *next-season* team defense (7.77 box vs 7.83 RAPM MAE). |
| **RAPTOR** (538) | Box + tracking + on/off hybrid | Same hybrid spirit; ours is a leaner box+tracking fit with an explicit turnover-weighted RAPM defensive term. |
| **EPM / LEBRON / DARKO** | Bayesian box⊕RAPM, per-stat stabilization, luck-adjustment | We share box-informed-RAPM anchoring. We **tested and rejected** per-stat shrinkage constants (didn't survive team aggregation) and luck-adjusting the residual (own FT%/3P% are too persistent to strip — corroborated by PIPM finishing 6/10 in public retrodiction). |
| **Win Shares / VORP** (BBR) | Box value allocated vs replacement | Our ≈Wins is the analogous above-replacement count. Cross-check: ≈Wins vs WS correlate **0.84 overall, 0.89 offense, 0.52 defense** — the divergence is WS's Defensive Win Shares being essentially team-defense-by-minutes (over-credits role players on good defenses). |

## 6. Honest limitations (please stress-test these)

- **Defense is the weak link.** Offense calibrates well (team-offense R² ≈ 0.88); defense does not (box-only R² ≈ 0.45, ≈ 0.48 with tracking). Perimeter *containment* produces few countable events even with tracking. All-Defense honors and several perimeter/matchup features were tested as fixes and **failed the win gate** (kept as UI badges, not inputs).
- **Aggregate accuracy does not beat the market.** Walk-forward wins MAE (mean absolute error — the average miss) is **7.58** vs the preseason betting market's **6.88**; the naive mean-reverted baseline is 8.13. The deliverable is *per-team disagreement with a structural reason* plus calibrated intervals, not beating the market on average.
- **Roster is the other irreducible error.** Backtests use reconstructed opening-day rosters (2016–17+); midseason trades/injuries are genuinely unknowable in October, which is most of the gap to the leaky upper bound (7.11).

*Full derivations, negative results, and the walk-forward protocol are in `DESIGN.md` and the module docstrings (`nbaproj/impact.py`, `aging.py`, `project.py`, `rapm_blend.py`, `simulate.py`, `carryover.py`).*
