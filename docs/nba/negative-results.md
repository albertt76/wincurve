# NBA — measured negative results (do not re-attempt blind)

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

### Measured negative results (do not re-attempt blind)

All of these were well-motivated ideas that **failed their gate**. Recorded so the effort
is not repeated.

| Idea | Result |
|---|---|
| Age × injury-history interaction | Right sign in 16/16 folds, worth 0.0001 MAE |
| Canonical minutes by within-team rank | 8.44 → 9.14 (worse) |
| Hybrid: canonical curve for newcomers only | 8.44 → 8.61 (worse) |
| Talent concentration / depth features | Null after controlling for quality; sign **opposite** to hypothesis |
| Coach rotation-concentration reshape | 8.34 → 8.54 (worse) |
| Absence-absorption adjustment (both forms) | worse; likely already in the calibration |
| Tanking adjustment from lottery reform | 2019 reform *tripled* tanking, not reduced it |
| Trimming "bloated" current rosters before aggregation | Fails the gate: any roster cap raises MAE (7.95 → 8.04 at cap-18); the minute-weighted mean already down-weights camp bodies, and the tail carries real signal |
| **Offensive-creation features** (potential/secondary assists, points created, screen assists) | Offense is already saturated: out-of-sample team-offense corr flat 0.930 → 0.930 (calibrated err 0.870 → 0.879, *worse*); win gate +0.018 (noise). Box assists + usage + efficiency already encode creation, so these are collinear refinements — Brunson barely moves (+2.09 → +2.21). Passing data pulled and kept (`player_passing`), features not added. |
| **`fta_p100` (and `ts_pct`) as the position-relative offense feature** (`gate_position_relative_offense.py`) | The Open item's named candidate `fta_p100` (rim-runner contact-finishing) is a near-**no-op** made position-relative (+0.005, positional gap 1.12 → 1.04). `ts_pct` (shipped 2026-08-06) works pure-box (+0.064) but is a **null in the full shipped pipeline** (−0.003) — the offensive RAPM blend already corrects its efficiency confound. **Both superseded by `oreb_p100`** (the offensive twin of the defensive `dreb_p100`, +0.051 full-pipeline 6/6), which removes the orthogonal rebounding-role confound. `ts_pct + oreb` OVERcorrects (centers below guards). Offense position-relative family = `oreb` only; do not re-add `ts_pct`/`fta_p100`. |
| Prior-year **All-Defense** correction to `def_impact` | Out-of-line bump of under-rated honorees toward an honor floor **fails the win gate at every setting** (−0.007 to −0.029, monotone with strength; `alldef_wingate`). As a plain team feature it also worsens out-of-sample team-defense (corr 0.733 → 0.726). Redundant at the team level — a team's defense is already the sum of its players' countable events. Kept as a display **badge**, not a projection input. |
| **All-NBA** as a star impact bump | Mechanically wrong, so not gated: All-NBA is largely an OFFENSIVE / reputational honor. The metric's low ratings of All-NBA guards are defense-driven and correct — Brunson is +2.09 off / −2.01 def, a real two-way wash — so bumping impact toward the honor would credit defense he doesn't play (or double-count offense, already R²=0.82). Display **badge** only. |
| **Re-fitting the box-vs-RAPM blend WEIGHT** (flat constants or walk-forward flat weight) | Rating-space proxy said flat ~0.5 wins +0.13/6-of-9; the real 5000-sim gate (`gate_blend_weight.py`) says otherwise — best candidates +0.02–0.03 wins, paired SE ~0.05–0.09 (noise), everything `w ≥ 0.5` worse, pure RAPM worst. The proxy sign-flipped (nonlinearity). 3-lens adversarial audit: negative-result-trustworthy. Box-metric fixes shrank RAPM's edge (pure RAPM 7.83 > pure box 7.77). **Flat-weight family closed.** |
| **Player-level / reliability-weighted RAPM blend** (`gate_player_level_blend.py`) | The completeness audit's two flagged formulations + variants, all gated real-sim: reliability-weighted team blend (−0.02 to −0.10), rating-space own-slope (−0.05), player-level box-informed (−0.09) and **pure-RAPM moment-matched (−0.07 to −0.21, the audit's literal ask)** — **none beats shipped 7.638**. Reliability is anti-correlated with turnover (−0.43), so it gets the sign backwards (leans RAPM on stable rosters the carryover already fixes); box-informed RAPM already possession-shrinks, so per-player blending double-shrinks (K2000 == pure box). Best move is always "less RAPM," never "redistribute per player." **Player-level family closed.** |
| **Turnover-conditional carryover** `rho(w)=rho0+rho1*w` (`gate_carryover_turnover.py`) | 538's Elo-memory-by-continuity trick applied to our carryover. Cheap pre-check ambiguous (right sign, 0.2% SSR gain, unstable LOSO); real 5000-sim gate **decisively worse every fold**: 7.638 → 7.692, −0.054 ± 0.053 SE, 0/6. Fitted rho1 unstable fold-to-fold (−0.83 to +0.04) — not enough residual pairs (~180) to identify a second free parameter this way. |
| **Luck-adjusted carryover residual** (LEBRON-style; `precheck_carryover_luck.py`) | Replace realized 3P%/FT% with league average before computing the residual to persist. Killed at the pre-check: luck-adjusted N−1 correlates **worse** with N's real rating (defense r² 0.306→0.284, offense 0.310→0.226) — own FT%/3P% are far more persistent than assumed (own FT% r²=0.32) while only the *allowed* side is mostly luck (opp 3P% r²=0.058); a blanket adjustment strips real offensive skill along with defensive noise. Joint regression: the "luck" component's coefficient (+0.45) is nearly as large as the "skill" core's (+0.59) — not noise. Corroborates PIPM (the one luck-adjusted metric in the public retrodiction table) finishing 6th of 10. |
| **Per-feature/shared shrinkage constant bump** (`gate_shrinkage_constant.py`) | DARKO/EPM-style per-stat stabilization constants. Stage 1 found no per-feature story — off_impact, def_impact, and combined impact all want the SAME higher shrink=600 vs shipped 200 (player-level MAE −1.4 to −1.6%). Stage 2 (real 5000-sim gate): doesn't survive team aggregation — **7.628 → 7.644, −0.017 ± 0.025 SE, 2/6 folds**, no coherent fold pattern. Classic player-metric-improves-team-doesn't, same shape as RAPM. **Kept shrink=200.** |
| **DRAYMOND-style all-category shot defense** (`gate_shot_defense_categories.py`) | Extended nearest-defender tracking from rim-only to all 6 shot-distance categories to attack perimeter containment. Stage 1 killed exactly the perimeter zones (3P r²=0.002, >15ft r²=0.010 — noise); only 2-pointers/less-than-10ft survived stability. Stage 2 real-sim gate: **decisively worse — 7.628 → 7.682, −0.054 ± 0.016 SE (~3.4 SE), 1/6 folds**. Root cause: the survivors are stable because they measure "is a rim-patrolling big" (Gobert/Embiid/Adams further inflated), and — unlike `dreb_p100` — are not position-relative standardized, so they reintroduce the exact confound that fix removed. **Stability alone is not a sufficient pre-check filter.** Data pull + merge code kept (unused by default); untested follow-up is position-relative standardizing these two features specifically. |
| **Position-relative shot defense** — the DRAYMOND follow-up (`gate_shot_defense_posrel.py`) | Added `p2_val`/`lt10_val` to `POSITION_RELATIVE_FEATURES` (standardized within position like `dreb_p100`), the fix the DRAYMOND rejection flagged. It **works as designed** — the confound is cleanly removed (corr with "is a center" +0.40 → −0.06; `def_impact`-vs-`dreb` 0.52 → 0.42), so unlike the league-wide version it does NOT hurt (−0.054 → **−0.025 ± 0.027 SE**, within noise). But it yields **no net gain**: the surviving signal is redundant with the shipped rim tracking (p2 +0.60, lt10 +0.72 with `rim_supp`/`rim_val`). Confirms the mechanism, doesn't ship. **Lesson: removing a confound stops the damage; it doesn't create signal that isn't there.** |
| **Player-level box→pure-RAPM refit with team FE** — root cause (`gate_defense_playerlevel_refit.py`) | Refit the box defensive metric at the PLAYER level against PURE RAPM (2013-25) with team fixed effects, instead of the team-defensive-rating target that makes box defense ~60% rebounds+blocks. Mechanism confirmed (dreb weight collapses, `def_impact`-vs-`dreb` 0.52 → 0.46), but the win is **circular** (the on/off feature `def_rating_rel` ≈ a crude RAPM carries most of it) and the honest box-only version is weak (player-level R²0.13) and **neutral-to-worse at the win level: 7.59 → 7.69 (no FE) / 7.65 (FE), within ~1 SE but negative, 3/6 folds**. blocks/steals do NOT rise (individual box stops are weak RAPM predictors). Same shape as the RAPM-swap / shrinkage-constant negatives — improves the player metric, dies at the team win number. |
| **Multi-season decayed RAPM** (`precheck_multiseason_rapm.py`) | Pool 2-3yr of stints with decay for a stabler RAPM. Killed at the pre-check: multi-season is ≤ single-season on the next-season team-defense predictive test in **all 6 variant-transitions** (box-informed & pure, two decays), and the box metric now out-predicts every RAPM variant (0.611 box vs 0.591 single vs 0.583 multi). At alpha=2000 single-season regulars already have ample possessions, so pooling injects staleness, not stability. Corroborates pure-RAPM 7.83 > pure-box 7.77 at the win level after this cycle's box fixes. |
| **nba_api matchup data for perimeter containment** (`precheck_matchup_defense.py`) | `MatchupsRollup` (who guarded whom, points/FG% allowed as primary defender). Feasibility is a clean positive (1 call/season, ToS-safe) so the pull is **wired and the data kept** (`nbaproj.ingest.matchups`, `data/processed/matchups.parquet`, 2017-18+). But the containment feature `m_fgpct` fails YoY stability even multi-season-pooled (r²≈0.05→0.08, well below usable) — DRAYMOND redux, because nearest-defender labels lack arm position/facing. The one stable feature (`m_tovp100`) restates steals; the other (`m_ptsp100`) is assignment-endogenous (hides weak defenders on weak scorers → Trae Young rates "elite"). No feature worth a gate. |
| **In-season model v2 — per-player talent update** (`gate_inseason_v2.py`) | Added a player-updated team arm to v1's rest-of-season blend (`a·SRS + b·player_updated + (1−a−b)·prior`): each player's through-N box production, scored with build_impact's own coefficients, k-blended into his preseason projection and re-aggregated by through-N minutes. Walk-forward fit drove **b→0** — N=25 b=0.00 (v2≡v1, 0/6), N=50 b=0.03 (−0.012, 0/6), churn subgroup no benefit. The team SRS already saturates the rest-of-season signal; the box-only player arm is defense-thin (compresses stars); and the through-N roster can't see a deadline acquisition, so v2's one theorized use case (post-deadline trades) isn't even reachable as built. Real at the player level, dies at the team-win number — same as the RAPM-blend and defensive negatives. Machinery + gate kept for the one untested redemption path (post-deadline current-roster snapshot). |
| **Injury-recovery offensive discount** (`gate_injury_recovery.py`) | An external review's third recommendation: a flat first-season-back offensive haircut, restricted to the immediate-return case (fitted on a clean n=155 cohort, delta_off −0.36 ±0.09 SE, real and offense-specific). Gated end-to-end, restricted to the ~76 team-seasons with a qualifying returning player: **decisively not helpful — delta −0.018 ±0.026 SE on affected teams, only 1/6 folds**. Same shape as the RAPM-swap / shrinkage-constant / in-season-v2 negatives: real at the player level, too few affected team-seasons per fold to move an aggregate built from 30 teams' worth of noise. Code kept (`nbaproj.rosters.apply_recovery_discount`), **not wired into the live pipeline** — `injury_returns.json` still restores to full basis level, no discount. |

**On the roster-"bloat" hypothesis (investigated, rejected).** The live July roster snapshot
carries 20–24 players and >290 mpg of prior-team minutes for ~8 teams (ATL 465 raw / 353
availability-weighted), which *looks* like it should drag those teams down. It does not, in
any fixable way: (1) the historical opening-day rosters the model trained on are *also* over
budget (median 272 mpg, 27–28/30 over) — the "bloat" is not unique to the current snapshot;
(2) the aggregation is a minute-weighted **mean** with a budget cap, so low-minute camp bodies
carry little weight — excluding the 3 `SUPPLEMENTAL_STATUS` players moves ATL only +1.6 wins,
and since every team has ~3 the *relative* effect is ~0; (3) capping the roster **fails the
walk-forward gate** (above); (4) the current aggregate distribution (mean −0.24, SD 0.33)
already matches the training distribution (−0.26, 0.33), so there is no scale artifact; (5)
no data bug — 587 roster rows are 587 unique players, zero cross-team double-counting. **ATL
31 vs market 45 is the defensive-metric weakness** (Dyson Daniels −0.41, Dort −2.01, NAW −0.45
— elite perimeter defenders the box score misses), i.e. a legitimate, explainable per-team
disagreement, which is the tool's actual deliverable — not a bug to trim away. This is what
motivated the RAPM integration test above.

**The recurring lesson:** three of these failed the same way — replacing team-specific
information with a league or career *average*. Prior-season minutes already encode a
coach's tendency; a canonical curve discards how a particular team distributes minutes.
Averages are the enemy here.

**Coach minute concentration IS a real trait** even though using it this way failed:
between-coach variance 0.00227 vs within-coach 0.00275, intraclass ratio **0.45**.
Thibodeau runs the league's most concentrated rotation (0.813 top-8 minute share over 9
seasons vs 0.750 mean); Daigneault 0.715 and Kerr 0.734 sit below it. Nick Nurse, contrary
to reputation, is at 0.729 — slightly *more* distributed than average. The untested case
where it should help is a **coaching change**, where prior minutes reflect the departed
coach.

#### ⚠️ Per-feature shrinkage/recency constants gated and REJECTED (2026-07-31)

The other half of the same deep dive (DARKO/EPM's per-stat stabilization constants): does
`off_impact`/`def_impact` want its own `shrink_minutes`/`recency_weights` instead of sharing one
(200, (5,3,2)) pair set before the off/def decouple existed? **Stage 1 (cheap, player-level
walk-forward projection MAE)**: swept shrink_minutes × 4 recency profiles separately for each
skill. Turned out NOT to be a per-feature story — off_impact, def_impact, AND the combined
"impact" all monotonically improve up to shrink=600 (recency staying at (5,3,2) for all three),
saying the *shared* constant is simply stale post-decouple, not that skills need different
treatment: off_impact 0.712→0.702 (−1.4%), def_impact 0.523→0.514 (−1.6%), impact 0.896→0.883
(−1.4%). **Stage 2 (`scripts/gate_shrinkage_constant.py`, real 5000-sim gate)**: the player-level
gain does **not** survive team aggregation — **7.628 → 7.644, −0.017 ± 0.025 SE, only 2/6 folds
improved**, no coherent fold pattern (unlike the position-estimator fix, whose gain concentrated
exactly where predicted). Classic "improves the player metric, dies at the team win number,"
the same shape as the RAPM finding. **Kept 200.** One harmless fix retained regardless: 
`project_next_season`'s `shrink_minutes` default used to bind at function-definition time (a
Python foot-gun), now resolves the module constant at call time — behavior-neutral at 200,
verified to reproduce the exact same MAE, but needed to make this gate testable at all.
