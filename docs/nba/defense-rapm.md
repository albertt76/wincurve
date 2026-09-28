# NBA — defensive metric and RAPM history

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

### Overview (from the old CLAUDE.md Architecture section)

**Offense and defense are decoupled** (shipped 2026-07, `decouple=True` in
`project_team_ratings`): each player's offense and defense is projected and aged separately and
calibrated against team offense/defense with its own slope, net = off + def. It is MAE-neutral
(gate: `scripts/gate_decouple.py`, 7.960 -> 7.957 excl. shortened folds, coverage 80.0 -> 80.7%)
but lets the app attribute a rating to offense vs defense and surface the defensive weakness.

Layer A's defensive component is the model's **weakest link**, and now we can say exactly why:
with box-score stats alone the defensive metric is ~60% defensive-rebounds-plus-blocks by fitted
weight (rebounds alone 41%), so `def_impact` correlated 0.82 with defensive rebounding — it
largely measured *being a center*, over-crediting non-defenders like Karl-Anthony Towns.

Three upgrades now attack that directly, all shipped:
- **Position-relative defensive rebounding** (`POSITION_RELATIVE_FEATURES`, shipped 2026-07):
  defensive rebounding is standardized *within position* (a center's rebounding vs other centers)
  instead of league-wide, so it stops being a 26%-weight proxy for "is a center". This is the one
  change that helped *both* accuracy and credibility: blend win MAE **7.74 → 7.62** (+0.11, ±0.055
  SE, 6/9 folds), the positional bias collapsed (mean def_impact was C +1.03 / G −0.33, now
  C +0.09 / G +0.04), guards flipped positive (Holiday −0.09 → +0.25, Anunoby, Dort) and backup
  centers dropped (Neemias Queta +0.24 → −0.68) — with Wembanyama still clearly #1 (+3.34), since
  blocks and rim protection stay cross-positional. See the position-relative section below.
- **Player-tracking defensive features** (`add_tracking_features`, shipped 2026-07): opponent
  FG% suppression *at the rim* with this player as the nearest defender, plus deflections and
  contested shots. Fixes the KAT-type over-credit (KAT's 2024-25 `def_impact` +1.62 → +0.44) and
  lifts recent-fold defensive calibration R².
- **Box-informed RAPM from play-by-play**, blended into the defensive aggregate by roster
  turnover (`nbaproj.rapm_blend`), and surfaced per-player in the UI (box-vs-RAPM "D↑/D↓" flags).

The tracking + RAPM steps move aggregate win MAE only within noise (their value is a *credible
per-player defensive number*); the position-relative rebounding fix moves it for real. **What
still slips through:** perimeter *containment* produces few countable events even with tracking,
so a few celebrated stoppers still rate modestly. Prior-year All-Defense was tested as a
correction for exactly this residual and **failed the win gate** (redundant at the team level);
it ships as
an eye-test **badge** in the UI instead of a projection input (see the All-Defense/All-NBA
section).

### ✅ Defensive metric / RAPM — SHIPPED as the turnover-weighted blend (how it got there)

*Status (2026-09-28):* this section is the history. RAPM shipped into the projection as the
turnover-weighted defensive blend (2026-07) and, 2026-08-06, an offensive blend; the
"did NOT ship (yet)" verdict below was the intermediate step.

The box-score metric captures only ~12-14% of franchise-level defensive variance; RAPM is
the documented fix. Built and validated this session:

- **Stint reconstruction** (`nbaproj/pbp.py`): use GameRotation for exact IN/OUT times
  (0.000 min error vs box score), play-by-play only for the score. Segment margins sum to
  the final score exactly on 5/6 test games.
- **RAPM estimator** (`nbaproj/rapm.py`): offense/defense ridge. Validated on synthetic data
  with known skills -- recovers offense (corr 0.92) AND defense (corr 0.90).
- **Box-informed RAPM** is the right architecture. Plain ridge over-shrinks anchors on
  partial data (rated Wembanyama's D at +1.2 vs box +4.3); shrinking toward the box prior
  fixes it (Wemby +4.65, AD +2.33) while still moving off it where stint data has signal.
  Team-defense reconstruction (in-sample, 350 games): box alone 0.44, plain RAPM 0.67,
  **box-informed RAPM 0.69**.

**Data blocker RESOLVED — switched to bulk play-by-play.** GameRotation was hard
rate-limited (stalled at 350/1230 games). Now `nbaproj/bulk_pbp.py` downloads the same
stats.nba.com play-by-play as static Apache-2.0 files from shufinskiy/nba_data (no key, no
account, no rate limit, all seasons 1996-97+) and reconstructs lineups offline. A full
season builds in **~50 seconds** vs the stalled live pull. Validated **RAPM-equivalent to
exact GameRotation stints (correlation 0.99)** on the 349 games where we have both. Two
reconstruction bugs found and fixed along the way: period-starter ordering (keep the
earliest five), and the stats.nba.com SCORE column being "away - home" not "home - away"
(which had silently swapped offense/defense — the fix took bulk-vs-exact correlation from
-0.15 to 0.99).

Full-season 2024-25 RAPM (plain, no prior needed at full data): Gobert #1 on defense, team-
defense reconstruction **0.83 vs box-score 0.68**. The box-informed variant remains the
architecture for partial/early-season data.

**✅ Cross-season predictive test PASSED (the non-circular one).** For 2021-22..2024-25
(4 seasons pulled from bulk), predicting each team's defense from its roster's PRIOR-season
defensive metric: RAPM beats the box score in all 3 transitions -- mean correlation **0.546
(RAPM) vs 0.401 (box)**. Out-of-sample, so not circular. RAPM defense is a genuinely better
predictor of future team defense. Indicative not decisive (only 3 transitions at the time;
2025-26 RAPM now exists too, via the v3 path below, so a 4th transition can be added). Script:
`scripts/rapm_predict.py`.

**✅ Integration test DONE — the deciding one. Verdict: RAPM did NOT ship (yet).**
(`scripts/rapm_integration_test.py`; box-informed RAPM pulled for 2013–2024, swapped for the
box `def_impact`, run through the full walk-forward gate. Box-informed RAPM is anchored on the
box prior, so it is scale-compatible and the swap is well-posed.)

- **Blanket swap is MAE-neutral: 7.96 → 7.92 (+0.04 wins, not significant), and 80% coverage
  slips 81% → 77%.** Yet RAPM *is* the better defensive metric. The reason it doesn't move the
  headline: the **one-year carryover is ~70% defensive and already absorbs the team-level
  defensive error RAPM would fix — they are SUBSTITUTES.** Turn the carryover OFF and RAPM
  improves MAE by **+0.32 (5/6 folds)**; turn it ON and the gain collapses to +0.04.
- **Complementary by DOMAIN, though.** The carryover persists a *team's* prior residual, so it
  is weak exactly when a roster turns over; RAPM attaches value to *players*, so it travels.
  By roster turnover (carryover ON): RAPM helps **high-turnover teams +0.31**, mildly hurts
  **stable rosters −0.36**. That is precisely the "Atlanta looks too low" case — a max-turnover
  team whose carryover is ~0, where the defensive metric (not roster bloat) is the real gap.
- **A blend weighting RAPM defense by each team's new-minute share** (a preseason quantity, not
  fitted): **7.96 → 7.80, +0.16 wins, 5/6 folds.** Robust to the weight (flat 50/50 ≈ 7.80),
  so it is largely generic ensemble benefit from two imperfectly-correlated defensive signals,
  with turnover-weighting as the mechanistic story. Modest and borderline (fold-level t≈3,
  team-level t≈1.45), about half the carryover's own +0.35.

**✅ SHIPPED as the turnover-weighted defensive blend** (`nbaproj.rapm_blend`,
`scripts/gate_rapm_blend.py`). The blanket swap was neutral because the carryover substitutes
for RAPM on stable rosters; the borderline +0.16 two-arm win-blend was superseded once defense
was decoupled. Blending the box and RAPM **defensive aggregates** by roster turnover inside the
one decoupled pipeline — `agg_def = (1−w)·box + w·rapm`, `w` = new-minute share, the defensive
slope calibrated on the blend — improves win MAE **7.96 → 7.77 excl-short, all 9/9 folds, with
equal-or-better coverage (80 → 81%)**. It beats both pure box and pure RAPM (both of which help
now that def is decoupled): box + carryover handle a steady roster, RAPM's player-level value
follows the churn the carryover can't. No second arm — it is one pipeline with a blended
defensive aggregate. The 2025-26 live input was unblocked by the PlayByPlayV3 reconstruction.
The what-if editor blends client-side too (per-player `defr` = projected RAPM defense, weight =
`team.turnover`), so edits stay exact.

#### ⚠️ Re-fitting the blend weight was gated and REJECTED (2026-07) — keep the turnover weight

Motivated by a rating-space sweep that suggested a flat `w ≈ 0.5–0.75` would beat the shipped
turnover weighting by ~+0.13 wins (6/9 folds), the flat and walk-forward-fitted weights were run
through the **real 5000-sim gate** (`scripts/gate_blend_weight.py`, paired seeds across schemes):

| scheme | exShort MAE | vs shipped |
|---|---|---|
| box (w=0) | 7.769 | −0.131 |
| **turnover [SHIPPED]** | **7.638** (7.62 under the repo default seed) | — |
| flat 0.25 | 7.614 | +0.024 (in-sample-best constant) |
| flat 0.50 | 7.678 | −0.040 |
| rapm (w=1) | 7.832 | −0.194 (worst) |
| w_fit (walk-forward flat weight) | 7.610 | +0.028 (honest re-fit; SE ~0.09, weights bounce 0.30–0.75) |

The best candidates gain **+0.02–0.03 wins with paired SE ~0.05–0.09 — within noise**; everything
at `w ≥ 0.5` is worse. **The rating-space proxy was unfaithful** (flat 0.50: proxy +0.075,
real-sim −0.045, a sign flip): the rating→wins map is nonlinear and win-MAE weights teams by the
local slope (steeper near .500), so a scheme that trims tail-team rating error looks good in
rating space and does nothing in wins. **This is the project paying a _third_ time for tuning on
an internal diagnostic instead of the downstream objective** — always gate in the simulation.
Adversarially audited (3 independent lenses: leakage, control-fidelity, completeness) →
unanimously *negative-result-trustworthy*; the shipped arm reproduces `agg_def_used` to 0.0, and
the low power (6 folds) means the honest reading is "re-fit **not shown to beat** shipped", which
defaults to keeping shipped. The deeper reason RAPM's edge shrank: the **box metric was itself
fixed this cycle** (position-relative rebounding + tracking), so pure RAPM (7.83) is now worse
than pure box (7.77) and the blend's headroom over box collapsed to ~0.13. **The flat-weight
family is closed — do not re-attempt.**

#### ⚠️ Player-level & reliability-weighted blends were gated and REJECTED (2026-07)

The completeness audit's two "still could win" formulations — plus their variants — were all
built and gated (`scripts/gate_player_level_blend.py`, real 5000-sim, paired seeds). **None beats
the shipped turnover blend (7.638):**

| formulation | best exShort MAE | vs shipped |
|---|---|---|
| **turnover [SHIPPED]** | **7.638** | — |
| **B** reliability-weighted team blend (`w` = minute-weighted mean of player RAPM `poss/(poss+K)`) | 7.661 | −0.023 (monotone worse as it trusts RAPM more) |
| **C** rating-space own-slope (each metric its own slope, turnover weight) | 7.686 | −0.048 |
| **A1** true player-level blend, box-informed RAPM | 7.729 | −0.090 (K2000 collapses to pure box) |
| **A2** true player-level blend, **pure** RAPM moment-matched (the audit's literal ask) | 7.709 | −0.071 |

Two clean lessons. **(1) The turnover weight puts RAPM in the right _places_, not just less of
it.** RAPM reliability (possessions) is **anti-correlated with turnover (−0.43)** — churned
rosters have thin-sample newcomers — so reliability-weighting leans on RAPM for *stable* rosters,
exactly where the one-year carryover already fixes team defense, and leans on box for *churned*
rosters, exactly where RAPM's player-level value was supposed to travel. It gets the sign
backwards. **(2) Box-informed RAPM is already a possession-weighted shrink of pure RAPM toward the
box**, so an explicit per-player possession blend (A1) double-shrinks — K2000 lands exactly on
pure box (7.769). Even pure RAPM (A2), which avoids that, only *approaches* the shipped number
from below as K rises (more shrinkage → less RAPM): the best move is always "use less RAPM," never
"redistribute it per player." Root cause is the same as the weight re-fit: after the box metric
was fixed this cycle, RAPM's marginal edge is only ~0.13 wins, and the turnover blend already
extracts it. **Player-level / reliability family closed — do not re-attempt.**

### ✅ SHIPPED: rim + hustle tracking into the defensive metric (`add_tracking_features`)

The box defensive fit now also sees player-tracking features, merged in `nbaproj/impact.py`
(`add_tracking_features`, wired through `scripts/stage2_report.py`):

- **rim_supp / rim_vol / rim_val** — opponent rim FG% *expected minus allowed* with this player
  as nearest defender (from `rim_defense.parquet`, 2013-14+), how often he defends the rim, and
  their product (a points-saved proxy, ~0 for low-volume perimeter players so "missing" reads
  neutral). Traded players' stints are combined into a season total (attempts/makes summed,
  expected % attempt-weighted) before rates are derived, so the merge stays 1 row per
  player-season.
- **defl_p36 / cont2_p36** — deflections and 2pt shots contested per 36 min (from
  `hustle.parquet`, 2016-17+).

Missing values (older seasons, or a player who never registers at the rim) get z-score 0 =
league-average, filled only for real rotation players (`has_rates`) so tracking is weighted
exactly like the box features. **Gate (`final_gate`, authoritative on the shipped parquet,
5000 sims): blend 7.771 → 7.735 excl-short, +0.035 ±0.059 SE, 6/9 folds — within noise on
aggregate.** The reason to ship is player-level: KAT 2024-25 `def_impact` +1.62 → +0.44,
Holmgren/Gobert/Wembanyama correctly on top, recent-fold defensive R² 0.33 → 0.48. On a *stable*
roster the UI's defense leans on the box component (RAPM only overrides under turnover), so this
fixes what the RAPM blend alone does not. **Follow-up:** the box-informed RAPM prior was fit on
the *old* box defense; refitting it on the rim-corrected box could recover a little more (the
gate above already shows the gain with the old-prior RAPM, so shipping now is conservative).

### ✅ SHIPPED: position-relative defensive rebounding (`POSITION_RELATIVE_FEATURES`)

The best defensive change of the batch — the only one that improved accuracy *and* credibility.
Diagnosis: even after tracking, defensive rebounding was the single largest defensive weight
(26%) and is mostly positional *role*, not skill (grab the ball after a miss). League-wide it made
centers average `def_impact` **+1.03** and guards **−0.33**, and put low-minute backup bigs
(Jonathan Isaac +3.24 at 15 mpg, Kevon Looney, Nurkić) atop the leaderboard.

Fix: standardize **only `dreb_p100` within (season, position-group)** — a center's rebounding vs
other centers — while blocks, steals, and rim protection stay league-wide (they are genuine
cross-positional defense; erasing them would wrongly demote elite rim protectors). Position group
(G/F/C) comes from `rim_defense.PLAYER_POSITION` (2013-14+; unknown → forward, which collapses to
league-wide). `_standardize_within_position` mirrors `_standardize_within_season` but groups by
position too; `build_impact` routes `POSITION_RELATIVE_FEATURES` through it.

Result (`scripts/final_gate`-style, 5000 sims): blend win MAE **7.735 → 7.622 excl-short, +0.113
±0.055 SE, 6/9 folds**, coverage held at 79% — ~2 SE, the first defensive step distinguishable
from noise. Player-level: positional bias **C +1.03/G −0.33 → C +0.09/G +0.04**; guards flip
positive (Holiday −0.09 → +0.25, Anunoby −0.09 → +0.21, Dort −0.05 → +0.23); backup-center
over-credit drops (Queta +0.24 → −0.68, KAT +0.44 → −0.49); **Wembanyama stays #1 at +3.34** (he
loses only the rebounding-inflated part of his old +4.38). Why it beats the earlier "can't fix
box defense by adding box features" prior: this isn't a new feature, it's *removing a positional
confound* from an existing one. (The RAPM box-informed prior used to still anchor on the pre-fix
box defense; that shortcut has since been closed — see "RAPM prior re-anchored on the current box
defense" below, +0.03 wins.)

#### ✅ SHIPPED: BPM position fallback for the 8 seasons with no listed position (2026-07-31)

An advanced-metrics deep dive verified a real defect in the step above: `pos_group` comes from
`rim_defense.PLAYER_POSITION`, which starts 2013-14, and any unknown position maps to `"F"`. So
for **8 of 21 backbone seasons (2005-06..2012-13 — 34% of the standardization pool)** every
player collapsed into one group and position-relative defensive rebounding silently degraded to
plain league-wide standardization for that third of the ridge's training data.

Fix (`nbaproj.impact._bpm_position_estimate`): BPM 2.0's published box-derived continuous
position formula — `position = clip(2.130 + 8.668·%TeamTRB − 2.486·%TeamSTL + 0.992·%TeamPF −
3.536·%TeamAST + 1.667·%TeamBLK, 1, 5)`, recursively shifted so each team's minute-weighted mean
is exactly 3.0 — computed from `player_team_seasons` (correctly attributes traded players; all
21 seasons available, no new data). Used **only as a fallback** for missing `pos_group`; the real
rim-tracking position (2013-14+) is never overridden. Validated against real listed positions
where both exist (6,819 player-seasons): a true guard is bucketed center only 4% of the time, a
true center bucketed guard 17% — the extremes separate well even though the "forward" middle is
naturally fuzzy (~60% 3-way accuracy), which is what matters for judging a center's rebounding
against other centers rather than guards.

Gate (`scripts/gate_bpm_position.py`, 5000 sims, paired seeds): win MAE **7.638 → 7.628 excl-short,
+0.011 ±0.0039 SE (~2.7 SE), 5/6 folds improved**; official-seed headline **7.62 → 7.61**,
coverage held (78.3% → 79.4%). Gains concentrate exactly as the mechanism predicts: largest in
2017 (+0.020) and 2018 (+0.018) — folds whose training history is almost entirely pre-2013 — and
smallest in 2024/2025 (+0.003, +0.007), which already draw mostly on post-2013 training with real
positions. Individual effect is small and mechanistically clean: correlation between pre- and
post-fix `def_impact` for scored rows (2013-2025) is 0.9995 (mean |Δ| 0.026), and the biggest
movers are 2013-scored players — whose calibration is 100% dependent on the newly-fixed
pre-2013 training rows, exactly as expected.

#### ✅ SHIPPED: RAPM prior re-anchored on the current box defense (2026-08-02)

The cheapest of the roadmapped defensive experiments, and it worked. The box-informed RAPM
parquets were being generated as a side effect of `rapm_predict.py`/`rapm_integration_test.py`,
with the box prior *as it stood then* — before position-relative rebounding, rim/hustle tracking,
and the BPM position fallback improved the box defensive metric. So the shipped RAPM arm was
anchored to a stale box, exactly the shortcut flagged (conservatively) in the sections above.
**`scripts/build_rapm.py`** is now the one canonical generator: for each season 2013-14+ it fits
ridge RAPM (alpha=2000) shrunk toward the **current** box `off_impact`/`def_impact` as its prior
(contemporaneous, point-in-time-safe), writing `data/processed/rapm_<season>_a2000.parquet`.

Gate (`scripts/gate_defense_prior.py`, 5000 sims, paired seeds): win MAE **7.628 → 7.591
excl-short, +0.036 ±0.017 SE (~2.2 SE), 5/6 folds improved**, coverage held (78.3 → 78.9%);
official-seed headline **7.61 → 7.58**. Biggest gains 2025-26 (+0.098) and 2018-19 (+0.073).
Reproducibility caveat: the NEW side reproduces via `build_rapm.py --refresh`; the OLD (stale)
prior is not reconstructable (its pre-fix box prior no longer exists), so the A/B was a one-time
measurement (recorded in `gate_defense_prior.py`). **Going forward, a box-metric change means:
regenerate `player_impact.parquet` → re-run `build_rapm.py` → rebuild bundles**, so the prior
never silently goes stale again.

#### ⚠️ DRAYMOND-style all-category shot defense gated and REJECTED (2026-07-31)

The third item from the same deep dive (FiveThirtyEight's DRAYMOND): our tracking defense pulls
only 1 of 6 `LeagueDashPtDefend` shot-distance categories (the rim). Pulled the other 5
("Overall", "3 Pointers", "2 Pointers", "Less Than 10Ft", "Greater Than 15Ft" — same endpoint,
same ToS surface, 2013-14+, now cached in `data/processed/shot_defense.parquet` via
`nbaproj.ingest.shot_defense_all_categories`, wired into `pull_all()`) to attack perimeter
containment, the metric's one remaining named weakness.

**Stage 1 (year-over-year player stability, ≥3 attempts/game both seasons) killed exactly the
zones that would have helped**, and this is worth internalising: 3-pointers r²=0.002 and >15ft
jumpers r²=0.010 — indistinguishable from noise. This corroborates rather than fixes "perimeter
containment produces few countable events even with tracking": Second Spectrum's nearest-defender
labelling has no arm position or facing direction, so a jump-shot closeout is far noisier than a
rim contest. "Overall" (r²=0.041) was dropped too — likely redundant with `rim_supp`, since it's
dominated by whichever shots a player defends most (mostly rim shots, for a rotation big). Only
**2-pointers (r²=0.106)** and **less-than-10ft (r²=0.174)** survived.

**Stage 2 (`scripts/gate_shot_defense_categories.py`, real 5000-sim gate) — decisively worse, not
neutral**: **7.628 → 7.682, −0.054 ± 0.016 SE (~3.4 SE), only 1/6 folds improved**. The reason is
exactly what should have been checked before Stage 2 and wasn't: the survivors are stable
**because they mostly measure "is a rim-patrolling big,"** not because they capture a new skill —
the biggest movers are Rudy Gobert (5 of the top 8, further inflated +1.0 to +1.4), Joel Embiid
(+1.09), and Steven Adams (+1.03), the exact profile `POSITION_RELATIVE_FEATURES` was built to
correct. Because `p2_val`/`lt10_val` are **not** position-relative standardized (unlike
`dreb_p100`), adding them reintroduces the confound the earlier fix removed. **The lesson: a
year-over-year-stable feature can still be a reliable measurement of the wrong thing (position),
not skill — stability alone is not a sufficient pre-check filter.**

**Rejected, but the data pull and the merge code are kept.** `shot_defense.parquet` is a clean,
already-cached, ToS-safe dataset regardless of outcome. `nbaproj.impact.SHOT_DEFENSE_SURVIVING_
CATEGORIES` / `SHOT_DEFENSE_FEATURES` / `add_tracking_features`'s `shot_defense` parameter follow
the same optional-tracking-input pattern as rim/hustle, but `scripts/stage2_report.py`
**deliberately does not pass it** — the shipped model does not use these features. **One live,
untested follow-up**: standardizing `p2_val`/`lt10_val` *within position* (adding them to
`POSITION_RELATIVE_FEATURES`, exactly like `dreb_p100`) targets the identified failure mode
directly and might redeem them — not attempted here, and not assumed to work just because the
earlier dreb fix did.
