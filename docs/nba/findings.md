# NBA — findings, investigations, and corrected claims

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

### ✅ INVESTIGATED & RESOLVED: the "bloated summer rosters" hypothesis was WRONG

The market comparison flagged that `commonteamroster` in July returns **20-24 player
rosters** — camp invites, two-ways, and just-acquired players still carrying their *old*
team's minutes — so 8 of 30 teams supply **>290 player-minutes/game** against the 240 budget
(ATL 353, UTA 342, MIL/MEM/WAS ~320). The natural worry was that the minute-weighted **mean**
aggregation (`scripts/project_current.py`) lets negative-impact camp bodies drag the aggregate
down, making ATL project to 31 vs the Kalshi market's 45 (and similarly UTA/MIL/MEM/WAS/CHA/
PHI/DAL). **That worry was investigated in full and rejected** — see the roster-"bloat" entry
in the negative-results table below for the evidence. In short: the historical training rosters
are *also* over budget (median 272 mpg), the minute-weighted mean already down-weights camp
bodies (excluding them moves ATL only +1.6 wins, ~0 relative), **capping the roster fails the
walk-forward gate (7.95 → 8.04)**, and there is no scale or data bug. **ATL 31 vs 45 is the
defensive-metric weakness** (Dyson Daniels, Dort, NAW — elite perimeter defenders the box score
underrates), i.e. a *legitimate* per-team disagreement, which is the tool's deliverable. So the
flagged gaps **can** be read as findings, with the defensive-metric caveat. The UI's **Minutes
supplied /240** stat stays as useful context. This is what motivated the RAPM integration test
(above); RAPM is the structural fix for exactly these high-turnover teams, though it did not
clear the aggregate gate. `roster_opening_day` does **not** trim to ~15 — it keeps ~19, same
as the live snapshot; that earlier claim was mistaken.

### The alpha reversal (worth internalising)

Ridge alpha was set to 10, then lowered to 1 because that made the contemporaneous
aggregation slope land near its theoretical value of 5. **That was optimising the wrong
thing.** Chosen by downstream projection correlation instead:

| alpha | 1 | 5 | 10 | 25 | 60 |
|---|---|---|---|---|---|
| downstream corr | 0.595 | 0.652 | 0.678 | **0.698** | 0.698 |

Now **25**. Worth 0.5 wins of MAE in realistic mode and 0.68 in the leaky bound. The
weakly-regularised metric was over-dispersed — projected team aggregates had SD 0.926
against an actual 0.829, when a properly shrunken estimator must be *narrower* than
reality. Heavier shrinkage also reduced the defensive positional bias as a side effect
(correlation with rebounding 0.96 → 0.83).

**This project has now paid twice for tuning against an internal diagnostic instead of
the downstream objective** (the other time: shrinkage strength in Stage 2b). Always
tune against measured end-to-end error.

### Stage 4-5 bugs found (both were silent, both mattered)

- **Cartesian merge.** Joining simulated results to market data on `team_id` without
  `season_start` produced 630 rows per season instead of 30, comparing every season's
  win total against one season's results. It made the market look like MAE 11.7 instead
  of its known 6.67, and produced a fake "beats market by 2.3 wins". The row-count
  assertion now in `stage45_report.py` exists so this cannot recur silently.
  **Lesson: a result that beats a known-good baseline by a wide margin is a bug report,
  not a success.**
- **Calibration distribution mismatch.** The impact→rating slope was fitted on
  *contemporaneous* aggregates and applied to *projected* ones. Projections are far less
  dispersed (deliberately — shrinkage), so the mismatch produced predictions with spread
  1.14 against an actual 5.02: every team near .500. Fixed by fitting the calibration on
  projected aggregates from earlier folds. Worth 0.7-1.1 wins of MAE.
  **Lesson: fit a calibration on the same kind of quantity you apply it to.**

### Franchise effect — RESOLVED (four analyses + adversarial verification)

The apparent "franchise effect" is **one-year memory, not organizational quality**, and it
is now shipped as a carryover term.

- **No permanent component.** The residual autocorrelation decays like AR(1) (lags:
  +0.35, +0.12, -0.10, ~0) — a permanent franchise trait would stay flat. ML variance-
  components point estimate for the permanent SD is **zero**; a true 1.08 is marginally
  rejected. OKC and Boston are max-of-30 selection artifacts.
- **The "1.08 pts/100 = 2.9 wins" figure was wrong** — it read a lag-1 autocorrelation as a
  constant level, treating serially-correlated residuals as independent. Retracted.
- **The "0.41-0.63 wins" was an in-sample number.** The *permanent-mean* estimator is worth
  ~0.04 wins walk-forward. But the **one-year carryover** form reproduces the value as
  persistence: **+0.35 wins end-to-end**, verified below.
- **It is partly a quality confound after all.** The original test used our own projection
  (orthogonal by construction, no power). Against the external betting market, ~1/3 of the
  effect is a quality confound — but the market stays diagnostic-only (never a feature).
- **~70% of the residual is defensive**, and the projection captures only ~12-14% of
  franchise-level defensive variance vs ~82-90% offensive. Invariant to the ridge sweep and
  feature deletion within the box-score family. **This is the trigger for the RAPM upgrade.**

### ✅ SHIPPED: one-year residual carryover (`nbaproj/carryover.py`)

`adjusted_pred[N] = pred[N] + rho * residual[N-1]`, rho fitted walk-forward on prior
residual pairs (lands ~0.36), suppressed after a shortened prior season. Gated end-to-end
through the simulation in roster mode (`scripts/gate_carryover.py`):

| | MAE (wins) | 80% coverage |
|---|---|---|
| Baseline roster mode | 8.30 | 77.0% |
| **+ carryover** | **7.95** | **79.6%** |

+0.35 wins excluding shortened-prior folds, improving 4/5 folds where it fires. This is the
first change all session that improves the backtest **and** survived adversarial
verification. Winsorizing extreme prior residuals was tested and does **not** help (cap at
8 is worse), so the tail residuals — e.g. Detroit/Charlotte, which beat the roster model by
~10 net-rating points in 2025-26 — are kept at full strength. Those large live adjustments
are the measurement-error signal, shown per-team in the UI.

#### ⚠️ Two carryover reshapes gated and REJECTED (2026-07-31) — keep the flat rho

A deep dive on advanced player-projection systems (EPM, LEBRON, DARKO, SCHOENE, BPM, CARMELO)
surfaced two ideas that reshape the carryover itself rather than swap in an external metric —
both were built and gated, both failed.

**Turnover-conditional rho** (`scripts/gate_carryover_turnover.py`). Motivated by 538's Elo model
varying its memory weight by roster continuity: fit `rho(w) = rho0 + rho1*w`, `w` = the *target*
season's roster turnover (the same `new_minute_share` the RAPM defensive blend already uses),
instead of one constant. A cheap OLS pre-check was ambiguous (right sign, but the interaction
explained only 0.2% of additional in-sample variance, the rho-by-turnover-quintile pattern wasn't
monotone, and leave-one-season-out estimates of the interaction swung from −0.12 to −0.57) — per
this project's rule that an internal diagnostic is not the verdict, it went to the real 5000-sim
gate anyway. Result: **decisively worse, every fold** — MAE 7.638 → 7.692, −0.054 ± 0.053 SE,
0/6 folds improved, run on the shipped config (RAPM blend on). The fitted rho0/rho1 are also
unstable fold to fold in exactly the way the pre-check warned. Not enough residual pairs (~180) to
identify a second free parameter this way. **Closed — do not re-attempt this form.**

**Luck-adjusting the carryover residual** (`scripts/precheck_carryover_luck.py`, killed at the
pre-check, never reached the sim gate). LEBRON's premise: replace realized 3P%/FT% with that
season's league average (attempt volumes stay real) before computing the residual the carryover
persists, since shooting variance is mostly luck. Tested directly on `game_log.parquet`: a
luck-adjusted season-N−1 rating correlates **worse**, not better, with season N's real rating —
defense r² 0.3055→0.2838, offense 0.3097→0.2256. Root cause, verified directly: **own** FT%/3P%
are far more persistent across a full season than the "it's mostly luck" premise assumes (own FT%
r²=0.32, one of the most persistent box-score rates there is), while what a team **allows** really
is mostly luck (opponent 3P% against r²=0.058, FT% against r²=0.013) — a blanket adjustment
strips real offensive skill along with defensive noise, and the offensive loss is larger. A joint
regression confirms it: the "luck" component carries a coefficient (+0.45) almost as large as the
"skill" core (+0.59) for predicting next season — it is not noise. Corroborates an independent
public data point from the same deep dive: PIPM, the only metric in the Dunks & Threes retrodiction
table built on luck-adjusted ratings, finished 6th of 10, behind plain BPM and RAPM. **Closed** —
a defense-only variant (never adjusting the offensive side) is the one un-killed variant, but it
would need to clear the same predictive-correlation bar first.

### ⚠️ CORRECTED: the tanking era claim does not hold

An earlier analysis claimed the 2019 lottery reform *tripled* tanking (-0.39 wins
pre-reform vs -1.70 post). **Both verifiers refuted it.**

- Era difference not reproducible: **-0.0044 +/- 0.0216 (t = -0.20, p = 0.84)** against the
  claimed t = -2.08.
- **The sign flips with an arbitrary cutoff** — bottom-4 gives *less* tanking post-reform
  (t = +2.67), bottom-10 gives more (t = -2.70). Only 31.5% of 108 specifications reach
  significance. A garden-of-forking-paths result.
- "All 7 post-reform seasons negative" was false on replication.
- No trade-deadline discontinuity — the test the mechanism specifically predicts — was run.

What survives: tanking is real but **smaller (~-0.5 to -0.85 wins)**, and the **win-rate
component is not distinguishable from zero**. The robust part is **margin** (-0.8 to -1.6
points per game over the last 27 games), supporting "tanking teams lose worse, not more
often." **The era comparison is a null: we cannot say whether the 2019 reform helped or
hurt.** So there is no usable evidence either way on whether the 3-2-1 reform will reduce
tanking — which still argues for a scenario switch rather than a baked-in adjustment.

### IMPLEMENTED: interval calibration fixed

`fit_rating_sigma` is now recency-weighted (3-season half-life), because pooling all prior
folds equally made the estimate stale and ~14% too narrow. Nominal 80% interval coverage:

| Mode | Before | After |
|---|---|---|
| Opening-day roster | 73.7% | **78.5%** |
| First-15-games | 73.7% | **80.7%** |
| Leaky bound | 77.8% | **83.0%** |

The 50% band also lands correctly (46-52% against nominal 50%). **Intervals can now be
trusted**, which is the precondition for any meaningful market comparison. Point accuracy
is unchanged, as expected — fixing interval *width* should not move the point estimate.

One caution recorded in `simulate.py`: a first attempt additionally inflated by a
season-level effective-sample-size correction, which overshot to 84.8%. Recency weighting
alone is sufficient.

### NOT IMPLEMENTED: absence absorption (real effect, no projection gain)

The measurement is unrefuted: teams absorb a player's absence at **0.65-0.71** of what
linear minute-weighted aggregation predicts, identically for stars and rotation players —
a general property of minute redistribution, confirming the Boston-without-Tatum intuition.

**But two faithful implementations both made projections worse**, so it is not applied:

| Implementation | Result |
|---|---|
| Availability-blind minutes + per-player discount | Best 8.397 at 0.68, but restructuring cost more (1.00 → 8.415 vs 8.343 before) |
| Availability-scaled minutes + upgraded filler value | Monotonically worse: 1.00 → 8.365, 0.68 → 8.427, 0.50 → 8.470 |

**Likely cause: double-counting.** The impact-to-rating calibration is *fitted* on
historical team-seasons in which players missed their normal share of games, so the fitted
slope already embodies average absorption. Correcting again subtracts a penalty that was
never applied.

`ABSENCE_ABSORPTION` defaults to 1.0 but is retained as a parameter, because it should
still matter for a **known long absence**, where a season-average calibration does not
apply — precisely the "Luka out for months" case. Untested there for lack of data.

Also unrefuted: concentration does **not** need to vary `sigma_rating` (justified spread is
1.6% across quintiles; star-injury risk is only 9% of residual variance), but
`sigma_rating` **is ~14% too narrow** from staleness, which matters ~10x more.

### Findings worth keeping

- **Conviction and robustness do not pick winners (measured 2026-09-28).** Over the 7 completed
  seasons, on every Blended gap of 1.5+ wins vs the preseason Vegas line, the model landed closer to
  the actual record on 45% / 42% / 42% of High / Medium / Low conviction gaps, and on 39% (n=87) of
  gaps that held under every robustness check vs 47% (n=74) of gaps that weakened — no better at
  matched gap sizes, worse in 5 of 7 seasons. Both are explanation layers (what a gap rests on),
  not signals of which disagreements will be right; the page says so and prints the live figures.

- **Win Shares cross-check (`scripts/compare_win_shares.py`).** Compared our ≈Wins to
  Basketball-Reference **Win Shares** (WS) for 2025-26 (bbref `/leagues/` advanced page,
  allowed). Overall correlation **0.84**, but split by side: **offense r=0.89 vs defense r=0.52**
  — the two metrics agree on offense and diverge on defense. The reason is structural: bbref's
  **Defensive Win Shares is essentially team defense allocated by minutes**, so it over-credits
  perimeter role players on good-defense teams (Brunson's gap is the league's largest, +7.3,
  almost entirely defensive — bbref +2.2 vs our −4.3) and under-credits players whose value is
  their own defense that our tracking features now catch (Dyson Daniels rates +1.8 for us; the
  rim/hustle shipping is *why* defense-r rose from 0.47 on the old metric). A uniform ~+1.8-win
  baseline offset (WS counts from zero, ≈W from replacement) sits under every gap. Our defensive
  win values are probably over-dispersed for guards (−4.3 for Brunson is too harsh in magnitude),
  the known defensive-metric weakness. Cross-check only, never a projection input.

- **Age × injury-history interaction: directionally real, practically useless.** The
  interaction coefficient had the hypothesised sign in **16 of 16** walk-forward folds
  (history matters more as players age), but adding it moved MAE from 0.1861 to 0.1860.
  Age matters directly instead: roughly -1 percentage point of availability per year.
- **The Stage 2 aggregation-slope anomaly was ridge over-regularisation**, not missing
  players. Two earlier explanations were wrong and are recorded as disproved in
  `impact.py`. Slope against theory's 5.0: 5.42 at alpha~0, 5.67 at 1.0, 7.01 at 10.0,
  10.06 at 50.0. Alpha lowered 10 → 1. **Open tradeoff:** alpha=1 improves team-level
  fit but widens individual impact to -14.4..+22.4 (wider than published metrics) and
  raises defensive impact's correlation with rebounding from 0.89 to 0.96. Final alpha
  should be chosen by downstream win-projection MAE once Stage 5 exists.

- **Peak age is ~25 for overall impact**, well before the popular 28-30 belief — and
  peaks genuinely differ by skill: three-point volume holds latest (~29), steals peak
  by ~22, and blocks decline from the very start. This vindicates modelling aging
  per-skill rather than as one curve.
- **Survivorship bias turned out small** (mean +0.05 points per 100 per year). Not
  luck: we deliberately leave season N+1 ungated, so players who declined into a
  reduced role are still measured. Most of the classic bias comes from requiring a
  large N+1 workload.
- **Shrinkage must target the age-mean, not replacement level**, at ~200 minutes of
  evidence. The first attempt (1200 minutes toward replacement) was *worse than
  reusing last season* and also made the aging term look harmful. When a component
  appears useless, check its constants before discarding the idea.
