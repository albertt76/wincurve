# NBA — offensive metric history

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

### ✅ SHIPPED: oreb offense + usage-weighted RAPM offense blend (2026-08-07)

The two Open items the 2026-08-06 offense work (below) left behind, both resolved as SHIPS. Both
change the deployed projection; the client-side recompute was re-verified bit-for-bit after both
(`winParts` JS vs the Python port: max |Δ| **5e-7** over 52 players on NYK/GSW/OKC; `computeRating`
reproduces every team's emitted rating within rounding), so roster edits stay exact. Combined
shipped MAE **≈ 7.39 excl-short** (within noise of the prior 7.40 — oreb's +0.05 net of the weight
switch's aggregate-neutrality).

**(1) `oreb_p100` REPLACES `ts_pct` in `POSITION_RELATIVE_FEATURES` (now `["dreb_p100",
"oreb_p100"]`).** The Open item asked whether a narrower feature (`fta_p100` etc.) beats the
weak/noisy `ts_pct` fix. The answer turned out to be `oreb_p100` — the exact offensive twin of the
shipped defensive `dreb_p100`, by identical logic: offensive rebounding is heavily positional ROLE
(a center crashes the glass), so league-wide it inflated centers' offense the way defensive
rebounding inflated their defense. Standardizing it within (season, pos_group) narrows the
center/guard `off_impact` gap **1.12 → 0.62 WITHOUT overcorrecting** (centers stay slightly above
guards), de-inflates pure offensive-rebounding specialists (Steven Adams −1.06 → −1.61, Gobert
1.13 → 0.74) and PRESERVES genuine offensive-center stars (Jokić stays 3.11, vs 2.70 under ts_pct).
It wins the gate cleanly: pure-box **+0.132 (6/6 folds, stable across 4 seeds)** vs ts_pct's +0.064
(3/6); and in the FULL shipped pipeline (offensive RAPM blend on) **+0.051 (6/6)** — exactly where
`ts_pct` collapses to a **null (−0.003, 2/6)**, because the offensive RAPM blend (shipped the same
day, 08-06) already corrects `ts_pct`'s efficiency confound but NOT `oreb`'s orthogonal
rebounding-role confound. So `ts_pct` was redundant in the shipped config and `oreb` carries the
real signal. Rejected alternatives: `fta_p100` (rim-runner contact-finishing) is a near-no-op
(+0.005, barely moves the gap); `ts_pct + oreb` OVERcorrects (centers drop BELOW guards, only 2 in
the top-30 offensive board) — matches the earlier `ts_pct + oreb` rejection.
`scripts/gate_position_relative_offense.py` (docstring + `main()`) now reproduces this. Protocol
followed: regenerated `player_impact.parquet` → re-ran `build_rapm.py --refresh` (the off_rapm
prior re-anchors on the new box offense; def unchanged — oreb is offensive) → rebuilt bundles + UI.

**(2) RAPM OFFENSE blend weight switches turnover → prior-season top-scorer share
(`nbaproj.rapm_blend.offense_blend_weight`).** The 08-06 blend used the same turnover weight for
offense as defense — a conceptual mismatch the review's completeness audit flagged: roster
continuity has nothing to do with whether a player is a high-usage creator, so on STABLE rosters
the turnover weight left stars at their box value while the league-wide off_slope recalibration
docked them ~1 offensive win each (Brunson/Curry/SGA all LOST vs the box method). Whether to trust
a team's box OFFENSE against RAPM is a question of shot-creation *concentration*, so the offense
weight now keys to the team's **prior-season top-scorer point share**; DEFENSE stays on turnover.
This roughly doubles the blend weight for stable star-led teams (Brunson 0.11 → 0.20, Curry
0.045 → 0.12, SGA 0.13 → 0.22), restoring each to ~box-level offense (the ~1-win loss erased:
tss−box ≈ 0). It is **aggregate-neutral** vs turnover (7.422 vs 7.436, −0.015 within noise; honest
prior-season ≈ contemporaneous, so no material leakage; tss on 5/6 folds vs 4/6) — the same "ship
on player-credibility at ~zero aggregate cost" shape as `ts_pct`/tracking-defense. Prior-season
(not contemporaneous) keeps it point-in-time-safe and knowable for the live projection. Wired
through the whole pipeline: `backtest_aggregates` (the calibration source), `project_current.py`,
`build_snapshots.py` all blend offense by `off_weight` and emit it per team; the two UI clients
(`ui/template.html`, `scripts/build_nba_players_ui.py`) blend offense by `team.off_weight` (falling
back to turnover for older bundles) with defense still on turnover. The original
`scripts/gate_rapm_offense_blend.py` sweep already showed turnover and top-scorer-share are
aggregate-neutral to each other; this re-check (the second Open item) judged them at the PLAYER
level, where top-scorer share wins.

### ✅ SHIPPED: position-relative offensive standardization + RAPM offensive blend (2026-08-06)
### ⚠️ NOTE: parts of this section were SUPERSEDED on 2026-08-07 (see the section above) — the
### `ts_pct` position-relative feature was replaced by `oreb_p100`, and the RAPM offense blend's
### turnover weight was replaced by prior-season top-scorer share. Kept for the historical record.

An independent NBA talent-evaluator review of the methodology + the full player export (see
`docs/nba_impact_methodology.md`) verified every data claim against the live export (exact match
to the decimal on position splits, individual player ranks, age buckets) and most code-structural
claims. Two of its three priority recommendations were genuinely novel — not already tried and
rejected per the extensive history below — cheap-to-moderate to build, and shipped. (The third,
swapping the defensive feature-acceptance gate to player-level RAPM/stability, turned out to
already be effectively in place as a pre-check, and the containment features that pre-check
rejected failed on measurement noise, not statistical power — see the matchup-data and shot-
defense rejections below. Not re-attempted.)

**Position-relative offensive standardization (`POSITION_RELATIVE_FEATURES`, now `["dreb_p100",
"ts_pct"]`).** The exact offensive mirror of the shipped defensive rebounding fix: `ts_pct` (true
shooting percentage) is now standardized within (season, position group) instead of league-wide,
so a center's efficiency is measured against other centers. Diagnosis matched the review exactly:
low-usage centers were getting near-star offensive credit for near-100% efficiency on assisted
rim finishes — Walker Kessler projected 8th league-wide by wins (ahead of Anthony Edwards, Kevin
Durant), with Jalen Duren/Daniel Gafford/Ryan Kalkbrenner/Neemias Queta similarly inflated.
League-wide the center/guard `off_impact` gap was **+0.55 / −0.57 (1.12 spread)**; `ts_pct` alone
shrinks it to **−0.04 / −0.38 (0.34)**. A second variant (`ts_pct` + `oreb_p100`) was tested and
**rejected**: it posted a bigger win-MAE number but *overcorrected* — centers rated *below*
guards (−0.45 vs −0.23), the opposite bias. `fg3m_p100`/`fg3_rate` were deliberately never tried:
centers rarely attempt 3s, so there's no positional inflation to remove, and standardizing a
near-empty reference group risks erasing genuinely earned skill (stretch-5 shooting) rather than
correcting a confound. `pts_p100`/`fga_p100`/`tov_p100` are usage-driven (a role choice, not
anatomy — Jokić/Embiid prove high usage is available to centers) and stay league-wide; `ast_p100`
was left untested (a passing big's assists are real rare skill, not an opportunity artifact —
position-relative treatment could amplify rather than remove signal, a candidate for a future
pass). Gate (`scripts/gate_position_relative_offense.py`, 5000 sims, paired seeds, 2017-2025):
win MAE **7.595 → 7.537 excl-short, +0.058 ±0.065 SE, only 3/6 folds** — weaker and noisier than
the analogous defensive fix. **Shipped anyway, for player-level credibility** (the win-MAE case
alone is not decisive) — the same precedent as `TRACKING_DEFENSE_FEATURES`, which shipped despite
a within-noise aggregate gain because it fixed specific, named player-level over-credits.
Remaining open item, per the user: try `fta_p100` and other narrower variants for a possible
cleaner win.

**RAPM offensive blend (`nbaproj.rapm_blend`, `nbaproj.rapm.build_rapm_impact(..., which=...)`).**
The shipped defensive RAPM blend never touched offense, and even the defensive blend was
comparison-only at the player level (a real code-verified gap the review flagged). The estimator
already computed `off_rapm` per player-season (same ridge as `def_rapm`) — it was simply discarded
downstream. `build_rapm_impact` now takes `which={"def","off","both"}`; `nbaproj.rapm_blend`
builds a third projection arm on the off-swapped table and blends `agg_off_used = (1-w)*agg_off_box
+ w*agg_off_rapm`, mirroring the defensive formula exactly with the **same turnover weight**
(`blend_weight(new_minute_share)`) — chosen over two candidate usage-based weights
(`top_scorer_share_weight`, a team's top scorer's point share, built free from data already on
disk; `USG_PCT`, sitting unextracted in the already-cached `player_advanced` pull) because the
turnover weight tied every variant's fold-improvement count or beat it. Gate
(`scripts/gate_rapm_offense_blend.py`, 5000 sims, paired seeds, defense held fixed at the shipped
turnover blend so only the offense change is measured): **every weight variant tried improved win
MAE** (turnover +0.11 to +0.15, `top_scorer_share` +0.10 to +0.12, majority of 9 folds each) —
pure RAPM offense alone barely moved it (+0.02 to −0.02), reproducing the defensive blend's own
signature (the blend beats both pure box and pure RAPM). That triangulation across independently-
built weight formulations, not any single variant's paired SE (~0.11–0.17, genuinely wide), is
what makes this a confident ship. Live wiring (`scripts/project_current.py`,
`scripts/build_snapshots.py`) mirrors the defensive arm exactly — `proj_off_rapm`/`rep_off_rapm`
alongside the existing `proj_def_rapm`/`rep_def_rapm`, an `offr` field in the per-player bundle
next to `defr` — and the client-side recompute (`ui/template.html`'s `computeRating`/`winParts`,
verified against the Python port in `scripts/build_nba_players_ui.py`) blends offense the same
way, so roster edits stay exact.

**Combined effect, both shipped**: win MAE **7.595 → 7.404 excl-short** (measured end-to-end in
the RAPM-blend gate's re-verification run, i.e. the offensive standardization fix already baked
into the baseline). Official-seed headline **7.58 → 7.40**. Player-level check, confirming both
fixes work as the review's mechanism predicted: Kessler's offensive ≈Wins dropped 4.05 → 1.26 (def
unchanged, 3.12 → 3.11 — the fix is offense-isolated, as designed); his RAPM offense (`offr`) is
*negative* (−0.95) against his positive box offense (+0.58), exactly the "box overrates a
low-usage rim-runner, RAPM is skeptical" pattern the review predicted. Brunson's RAPM offense
(+2.90) is far above his box offense (+1.68) — the "RAPM sees creator gravity the box misses" side
of the same mechanism — but his *displayed* ≈Wins did not move up on this spot-check (4.86 → 4.37,
driven down by the population-wide off_slope/replacement recalibration outweighing his own
personal RAPM boost, since NYK's turnover weight is only 0.11). Recorded here in full rather than
cherry-picked: the aggregate gate result is real and majority-fold, but a system-wide
recalibration can still move any *individual* named player either direction — exactly why every
change in this project ships on the aggregate gate, not on whether a poster-child example moved
the "expected" way.
