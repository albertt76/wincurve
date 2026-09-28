# NHL — team aggregation, carryover, and season simulation (Stages 4–5)

> Moved verbatim from `nhl/DESIGN.md` on 2026-09-28 so it is read on demand. The current
> status, shipped model, traps, and open items live in `nhl/DESIGN.md`; "above" / "below"
> may now point to a sibling file in `docs/nhl/`.

- ✅ **Stage 4 — team aggregation (DONE).** `nhl/aggregate.py` +
  `scripts/nhl_stage4_aggregate_report.py`: a team's even-strength rate above/below average is the
  **minute-weighted mean** of its skaters' xG-RAPM impacts (5 on ice always, so the ×5 is absorbed
  by the downstream calibration slope). **Validated:** the aggregate of *single-season* RAPM
  reconstructs team 5v5 xG-for/against at **r~0.92 off / ~0.88 def** (net vs xG-diff r 0.90-0.96 —
  the mechanism is sound); aggregating the *projected* (prior-season, `project(Y-1)`) impacts onto a
  team's actual roster/TOI predicts team 5v5 xG-differential at **r~0.65** (net), with ~88% of team
  minutes covered by a projection. **Replacement level DONE:** the uncovered ~12% (rookies/thin) have
  mean actual 5v5 RAPM net ~−0.03 (below average); filling them at replacement (weighting the
  aggregate over TOTAL team minutes, not just covered) lifts the projected net→xG-diff correlation
  **~0.65 → ~0.72 in 5/5 seasons** (`REPLACEMENT_OFF`/`_DEF`, the `fill=True` default). **Goalie
  GSAx projection DONE (and a defining NHL finding):** GSAx/60 barely persists year-over-year —
  **corr ~0.13, slope ~0.15** (2011-2024, 1500-min starters), vs skater offense ~0.62. Goaltending
  is nearly unpredictable in advance, so `goalies.project_gsax` regresses ~85% toward 0 (best
  projected starter ~+0.09) — projected goaltending barely differentiates teams; the skater
  aggregation carries the projection even though realized goalie variance swings standings (a big
  reason the NHL market is hard to beat).
  - **FIRST END-TO-END points projection (`scripts/nhl_stage45_points_report.py`).** project(Y-1)
    skaters → team 5v5 net → a walk-forward linear calibration to 82-game points. **At parity with
    the bar, not beating it yet:** MAE **10.76** vs naive **10.50** on 3 folds (Stage 1 bar 10.54) —
    the expected shape (the NBA's first cuts didn't beat the bar either). Projections are face-valid
    (CAR/TBL/FLA/EDM top, SEA/SJS/CHI bottom). It ties *despite* a leaky roster advantage, so the
    missing pieces carry real weight. **Remaining to beat the bar (Stage 4/5):** the **one-year
    carryover** (the NBA's single biggest lever, +0.35), special-teams (PP/PK), goalie level,
    impact→goals→points via goal-diff/Pythagorean, then the **season simulation** (regulation/OT/
    shootout) for the calibrated points *distribution*. NOTE: the current end-to-end is a LEAKY upper
    bound — actual roster + TOI; the honest version needs opening-day roster reconstruction + a
    minutes model (Stage 3b).
  - **One-year carryover — the big lever, exactly as in the NBA (`scripts/nhl_stage4_carryover_report.py`).**
    The roster projection misses persistent team effects (coaching/system/goalie that repeat), so add
    `rho · last-season residual`. Residual persistence measured at **AR(1) rho ~0.37, corr 0.38
    (n=181 pairs)** — essentially the NBA's ~0.36. Applied walk-forward it improves the points MAE
    **10.84 → 10.10 (−0.74)** and **beats the naive bar (10.44) and Stage 1 bar (10.54) for the first
    time**, mirroring the NBA where the carryover was the one change that cleared the gate. **Honest
    caveats:** only 3 evaluable folds and still the LEAKY-roster upper bound, so this is a *preliminary*
    beat, not a shipped number — it needs the honest (non-leaky) roster, more folds, and the full
    season-simulation gate. But rho=0.37 on 181 pairs is robust, so the carryover itself is real.
    **↳ UPDATE (Stage 3b, above): the honest, 9-fold re-run lands at 10.61 — the 10.10 was a favorable
    3-fold subset (leaky is 10.72 over the wider set), and removing the roster/TOI leak costs ~nothing.
    So the carryover + honest roster sit AT the bar; the decisive beat awaits the Stage 5 levers.**
- ✅ **Stage 5 — season simulation (DONE).** `nhl/gamesim.py` + `scripts/nhl_stage5_sim_report.py`:
  turn each team's projected strength into a standings-**points distribution** by simulating the
  games, replacing Stage 4's single linear `net → points` fit. Scored head-to-head on the EXACT same
  honest roster (Stage 3b), one-year carryover, and 9 full-season walk-forward folds — only
  strength→points changes.
  - **The game model** (all anchors measured on 2010-11..2025-26 `team_summary`). Projected 5v5
    **off → goals-for/game** and **def → goals-against/game**, calibrated *separately* (own slope
    each) and drift-tracked to the prior season's league scoring level (goals/game rose 2.66 → 3.08
    over the window; point-in-time safe, uses only year Y−1's actual league scoring). Splitting
    off/def with their own slopes is what lets defense carry its (lower) persistence weight — the
    single-`net` line couldn't. Each game: goals ~ **Poisson**, but the OT/SO rate is taken
    **empirically (~0.23)**, not from the Poisson tie mass (independent Poisson under-counts ties
    ~0.18, because hockey's score effects — leading team sits back, trailing team pulls the goalie —
    compress margins). So Poisson decides only *which team is better* (the conditional regulation win
    prob `w_reg`); the share going past regulation is the league constant; the OT/SO winner is
    `w_reg` shrunk hard toward a coin flip (a .70-pt% team wins only ~55% of its past-reg games,
    slope 0.38). Points are **2/1/0** — the loser point makes hockey points a **trinomial**, which
    the sim reproduces exactly and a win% model cannot. Validated in isolation: an average team →
    91.4 projected points (league 91.5), goal-diff→points slope 25.5 (measured 27.3), Monte-Carlo
    mean == closed form; season-luck SD ~8.5 points.
  - **Result — MAE-neutral, but it delivers the distribution (the point of the stage).** Over 9
    full-season folds: **sim + carryover = 10.50** vs the shipped **linear + carryover = 10.61**
    (−0.10, ±0.11 SE — **within noise**, ~1 SE, so genuinely MAE-neutral), vs the walk-forward
    **naive bar 10.83** (−0.33), vs the fixed **Stage 1 bar 10.54** (−0.04, i.e. the model now sits
    just past it). The neutrality is expected — goal-diff→points is near-linear (r=0.958), so a
    proper game model and a good line agree on the *mean*. Its genuine deliverable is the
    **calibrated points distribution**: the sim's season-luck spread (~8.5) is convolved with a
    projection-error term fit walk-forward on the sim+carry residual, and the resulting **nominal-80%
    interval covers 0.82** (target 0.80). Face-valid standings (2025-26 hindsight: CAR/VGK/TBL/LAK/DAL
    top, CHI/SEA/SJS bottom; `--detail 2025` prints every team's mean + 80% band + actual).
  - **Both remaining Stage 4 levers built and REJECTED — real signal, redundant with the carryover**
    (the NBA project's RAPM-vs-carryover finding, again). **Goalie GSAx into goals-against**
    (`goalies.team_gsax_per_game`): projected prior-season starter GSAx/60, regressed ~85% toward 0,
    subtracted from the GA rate. Prior-goalie GSAx correlates **0.21** with next-season points and
    the skater metric (5v5 shot-suppression) contains *no* goaltending, so it is genuinely additive —
    yet adding it **worsens MAE +0.06**. **Special teams (PP/PK)** (`team_st_goaldiff`): projected
    prior-season PP+PK net goal-diff/game, regressed to ~36%, shifted onto the goal differential. ST
    correlates **0.37** with points but its **YoY persistence (0.36) ≈ the carryover rho (0.37)** —
    the tell — so **+0.05 MAE**. Both are *persistent team traits* the one-year carryover already
    absorbs; the explicit terms (heavily regressed, hence noisy) only double-count. Kept in the code
    as gated, documented negatives (the `simG`/`simS` columns in the report).
  - **Honest conclusion.** The levers that were supposed to *decisively* clear the bar are redundant
    with the carryover, so the model sits **at** the bar (10.50 vs 10.54; −0.33 vs the per-fold
    naive). That is the expected shape for a high-parity, high-luck league where the market sits near
    the achievable frontier — the deliverable is per-team *disagreement* + the calibrated interval,
    not aggregate-MAE dominance (exactly the NBA project's honest read). Documented refinements not
    attempted: real strength-of-schedule (a balanced schedule is assumed — SOS is second-order and
    the per-season shift gaps complicate exact reconstruction), and a live 2026-27 projection (needs
    a current-roster feed — Stage 6).
