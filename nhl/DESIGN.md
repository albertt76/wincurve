# NHL season-points projection — DESIGN

Project each NHL team's regular-season **standings points** for an upcoming season as a
probability distribution, built bottom-up from the current roster's players, and compare
against betting/prediction markets to find explainable per-team disagreements. Same
research goal and method as the NBA project (`../DESIGN.md`, `../CLAUDE.md`) — **not a
betting tool** — adapted to hockey.

All statistical acronyms are expanded on first use, per the user's standing preference
(see root `CLAUDE.md`). This applies to script output too — reports carry their own legend.

---

## Why hockey is not just "basketball with a puck"

Four structural differences drive every modeling choice here:

1. **The currency is points, and the league mean is not 0.500.** A game past regulation
   awards **3** total standings points (2 to the winner, **1** to the overtime/shootout
   loser — the "loser point", OTL) instead of 2. So the league-average **point percentage
   (pt% — points / (2 × games played), our [0,1] modeling rate)** sits at **0.558**, not
   0.5 (measured, 2005-06..2025-26). Mean-reversion is centered on the training-set mean,
   never a hard-coded 0.5.
2. **Goaltending is a separate, volatile module.** A goalie's **GSAx (goals saved above
   expected — actual saves vs post-shot expected goals)** swings standings and is far less
   stable year to year than skater value. It gets its own projection + aging, not lumped
   into skaters. Nothing in the NBA model is analogous.
3. **Special teams are rated apart from even strength.** Power play (PP) and penalty kill
   (PK) use different personnel and have different value; MoneyPuck splits every stat by
   `situation` (5on5 / 5on4 / 4on5), which is what makes this clean.
4. **More parity / more luck than the NBA.** The fitted one-year reversion coefficient is
   **k ≈ 0.52** (below the NBA's 0.62): NHL teams keep only ~52% of their distance from the
   league mean year to year. That means the market is closer to the achievable frontier and
   harder to beat on aggregate — which sharpens the deliverable toward *per-team
   disagreement*, exactly as in the NBA project.

---

## Architecture (bottom-up, mirrors the NBA layers)

```
A. Skater impact       xG-based RAPM from play-by-play + shift charts, off/def decoupled,
                       even strength vs special teams; aging, shrinkage
B. Goaltending         GSAx projection + aging (separate module)
C. Minutes / roles     TOI (time on ice) budget per game, line/pairing roles, availability
D. Team aggregation    skaters + goalie + special teams -> team goals-for / goals-against rate
E. One-year carryover  + rho * last-season residual (as in the NBA model)
F. Season simulation   real schedule; regulation/OT/shootout branch -> POINTS distribution
G. Market comparison   season points over/under, Cup/division odds (downstream only)
```

**RAPM (regularized adjusted plus-minus — a ridge regression crediting a player's on-ice
impact while controlling for teammates, opponents, and zone starts)** on **xG (expected
goals)** rather than raw goals, because goals are too sparse in a low-scoring sport. This is
the direct analog of the NBA play-by-play → RAPM pipeline. Inputs: **shift charts** give the
on-ice 5-man units (`playerId`/`startTime`/`endTime` per shift) and **play-by-play** carries a
`situationCode` (strength state); MoneyPuck supplies the shot xG. **Availability floor differs
from xG:** the `/shiftcharts` endpoint returns data only from **2010-11 on** (2007-08/08-09/09-10
come back empty — empirically probed 2026-08), while MoneyPuck xG starts 2007-08. So the **RAPM
backbone is 2010-11 → 2025-26** even though xG/points go back further (see Stage 0).

**Why bottom-up:** ~21 seasons × ~30 teams ≈ 640 team-seasons is far too few to fit a
team-level model with many features; ~85k skater-season rows (MoneyPuck) is not. Same bet
as the NBA project.

---

## Stage 0 — data layer (DONE)

Cached, throttled, point-in-time pulls via `core.httpcache.HttpCache` (the shared,
sport-agnostic client) into `data/nhl/processed/` (gitignored; regenerate with
`python scripts/nhl_fetch_all.py`). Verified inventory:

| Dataset | Rows | Seasons | Span |
|---|---|---|---|
| `team_reference` | 62 | — | all franchises (stable `franchise_id`) |
| `season_index` | 109 | 109 | 1917-18 → 2026-27 (rule flags per season) |
| `team_summary` | 644 | 21 | 2005-06 → 2025-26 (records, points, PP/PK) |
| `moneypuck_teams` | 2,920 | 19 | 2007-08 → 2025-26 (team xG by situation) |
| `moneypuck_skaters` | 85,615 | 19 | 2007-08 → 2025-26 (skater xG by situation) |
| `moneypuck_goalies` | 8,950 | 19 | 2007-08 → 2025-26 (goalie xG / GSAx inputs) |

Availability windows (empirically verified, do not assume) — there are **two floors**:
MoneyPuck xG + team records start **2007-08** (so the **xG/points backbone is 2007-08 → 2025-26**),
but the NHL `/shiftcharts` endpoint (the on-ice units RAPM needs) is **empty before 2010-11** —
probed 2026-08, every 2007/2008/2009 game returns an empty `data` array — so the **RAPM/shift
backbone is 2010-11 → 2025-26** (`FIRST_SHIFT_SEASON`; constants and the fail-loud guard in
`nhl.ingest`). Team-summary records are pulled back to 2005-06 only to give the walk-forward
earlier training years. (Pre-2010 shift data would need the messier NHL HTML shift reports; deferred.)
Per-game play-by-play + shift charts (the RAPM bulk pull, thousands of games) are deferred to the
impact stage; structure verified (`nhl.ingest.game_pbp` / `nhl.ingest.shifts` / `nhl.ingest.roster`).

---

## Stage 1 — the bar (DONE)

`python scripts/nhl_baseline_report.py`. Walk-forward (predict season N from seasons < N,
reversion `k` refit per fold), 2010-11..2025-26, 492 team-seasons, errors in
**82-game-equivalent points** (pt% × 164):

| Baseline | MAE (points) | RMSE (points) |
|---|---|---|
| Previous points (persistence) | 12.09 | 15.13 |
| **Mean-reverted previous points ← THE BAR** | **10.54** | 13.20 |
| League-average points (flat) | 12.45 | 15.41 |

**MAE = mean absolute error** (average miss, direction ignored); **RMSE = root mean squared
error** (punishes big misses harder). Every later stage must beat **10.54** walk-forward or
it does not ship. Context: observed 82-game points SD ≈ 15.1; an approximate binomial noise
floor (upper bound — it ignores the loser point, which lowers variance) is ~7.0 points MAE,
so the achievable frontier is roughly there and the market will likely sit close to it.

---

## Stage 2 — impact metric (built + face-validated 2023-24)

Two estimators, both validated on 2023-24 before any team wiring (`scripts/nhl_impact_report.py`):

- **Skater xG-RAPM** (`nhl/rapm.py`) — reconstruct 5v5 stints from shift charts
  (`nhl/shifts.py`: 41 min of even-strength time/game, exactly 5 skaters a side), attribute
  each MoneyPuck 5v5 shot's xG to its stint, and ridge-regress xG-for-per-60 on on-ice
  offensive + defensive skater dummies plus a home term (`alpha=3000`, 400-min TOI floor).
  Output per skater: `off` / `def` / `net` (xG/60). **Face validity: Nathan MacKinnon #1 on
  offense** (the season's MVP), Panarin top net, Matthews/Tkachuk/Hyman high; Couturier /
  Luostarinen / Eichel lead defense. Stable across `alpha` 1500-5000.
- **Goalie GSAx** (`nhl/goalies.py`) — expected goals against − actual, from MoneyPuck.
  **Face validity: Connor Hellebuyck #1** (+33 GSAx) — the actual 2023-24 Vezina winner —
  then Demko, Swayman, Bobrovsky. Correct on the first run.

**Data pull** (`scripts/nhl_fetch_shifts.py`): the per-game shift charts are the heavy
input (~1300 games/season, one cached call each, idempotent/resumable). MoneyPuck's zipped season
shot file (xG, no on-ice IDs) is the response, joined to shifts by `full_gid = season*1e6 + game_id`.
The full pull is **2010-11..2025-26** (16 seasons — the `/shiftcharts` floor, not 2007-08; see Stage
0). An empty season now fails loud instead of writing a 0-row parquet. **Known per-season shift
gaps (do not mistake for bugs):** 2024-25 is missing shift charts for a contiguous 57-game block
(gids 2024021235..291 — genuinely absent from the endpoint, not a fetch failure), so its RAPM uses
1255/1312 games; 2019-20 carries ~1129 shifts (0.13%, 174 games) with an empty `endTime`, which
`nhl.shifts._abs_seconds` parses to NaN so the `end > start` filter drops just those shifts rather
than crashing the season.

**Impact viewer UI** (`ui/nhl/`, `scripts/nhl_build_impact_ui.py`) — a self-contained web
leaderboard of the Stage-2 metrics, following the NBA `ui/build.py` convention (a `__DATA__`
placeholder inlined into a single `impact.html`, no external deps). Per season: a sortable,
searchable skater xG-RAPM table (off/def/net, enriched with position + team, sign-colored with a
centered net bar) and a goalie GSAx table, with a season selector, Skaters↔Goalies toggle, a
**team filter** (per-season dropdown, matching the NBA players page), a **position filter that
separates C / F / D** (F = wingers L+R; the exact R/L/C/D stays as the tag next to each name), and
light/dark themes. In the shared cross-league nav this page is **"NHL → Players"** (the NHL analog
of the NBA players leaderboard; renamed from "Impact" 2026-08-05). Each season's RAPM fit is cached
to `impact_<yr>.parquet`; the build auto-detects available seasons, so re-running it as the pull
lands adds seasons for free. This is a **measurement** viewer (Stage 2), not the team-projection UI
(the NHL "Records" page, Stage 4-6). Rebuild: `python scripts/nhl_build_impact_ui.py` (view by
opening `ui/nhl/impact.html`).

**Known single-season caveats (the documented upgrade path, mirroring the NBA project):**
xG-RAPM over one season over-credits depth players who skate with elite linemates (e.g.
Foegele/Carrier with the Edmonton stars) and can't fully separate a forward line that always
plays together. Fixes, in order: pool **2-3 seasons** for stability, then a **box-informed
prior** (shrink RAPM toward a box/tracking estimate on thin samples) — exactly the arc the
NBA project's RAPM took. Aging curves + shrinkage and the skater/goalie **projection**
(not just measurement) are the rest of Stage 2-3.

## Statistical traps (handled; do not regress)

- **pt% mean ≠ 0.5** — the loser point inflates it to ~0.558. Reversion centers on the
  training mean, not 0.5. (`nhl/baselines.py`)
- **Shortened seasons** — 2012-13 (48 games, lockout), 2019-20 (68-71, varying **by team**,
  covid), 2020-21 (56, covid). Handled by modeling pt% (a rate); flagged in
  `SHORTENED_SEASONS`, to be reported separately in any raw-points/market comparison (as the
  NBA project does for its 66/72-game seasons).
- **Franchise spine** — join on `franchise_id`, which bridges Atlanta Thrashers ↔ Winnipeg
  Jets (35) and Phoenix ↔ Arizona (28). **One landmine:** the 2024 Arizona → Utah move is
  filed by the NHL as a *new* franchise (40 ≠ 28); bridged manually in `nhl/teams.py`
  (`FRANCHISE_BRIDGE`) so carryover follows the roster. Expansion teams (Vegas 2017-18,
  Seattle 2021-22) correctly have no prior season.
- **Points are not binomial** — a game yields 0/1/2 points (a trinomial with the loser
  point), so the binomial noise floor is only an approximate upper bound. A proper trinomial
  floor and the points-distribution simulator are later work.
- **PDO regression** — a team's shooting % plus save % ("PDO") reverts hard toward 100; the
  hockey analog of the NBA shooting-luck issue. To be handled as a known regression target in
  the carryover, not baked in blindly (the NBA project's luck-adjustment experiment is the
  cautionary precedent).
- **MoneyPuck legacy team codes** — through 2019-20, MoneyPuck coded four teams with dotted
  abbreviations (`L.A`/`N.J`/`S.J`/`T.B`) instead of the NHL tricodes (`LAK`/`NJD`/`SJS`/`TBL`);
  it switched to tricodes by 2020-21. These appear in BOTH the shot file (`teamCode`/`homeTeamCode`)
  and the season-summary `team` column. Un-normalized, ~13% of pre-2021 5v5 shots failed the
  team-id join in `rapm.attach_xg` and were misattributed to the OTHER on-ice team — and because
  New Jersey has the lowest `team_id` (always "team0"), 100% of NJD's offense was silently credited
  to its opponents. Fixed by `rapm.MP_CODE_ALIASES` (normalize on every code column, then a
  fail-loud assert on any leftover unmapped code), applied in both `attach_xg` and the viewer's
  team-tag enrichment. **Do not regress:** any new MoneyPuck join must normalize through that map.

---

## Roadmap

- ✅ **Stage 0** — data layer: cached, throttled, point-in-time pulls; verified inventory.
- ✅ **Stage 1** — baselines: **the bar = 10.54 MAE points** (mean-reverted previous points),
  persistence 12.09, flat 12.45; k ≈ 0.52.
- 🟡 **Stage 2** — impact estimators **built and face-validated** (2023-24); **impact viewer UI
  shipped** (`ui/nhl/`, sortable skater xG-RAPM + goalie GSAx leaderboards, season-selectable);
  full multi-season shift pull is **2010-11..2025-26** (the `/shiftcharts` floor — 2007-2009 have
  no shift data); aging/shrinkage and turning measurement into projection still to come. See
  "Stage 2 — impact metric".
- 🟡 **Stage 3 — impact refinement (in progress).** Turn the single-season *measurement* into a
  stabler forward-looking talent estimate. Two steps validated (walk-forward: predictor through Y-1
  vs each player's actual single-season net in Y), packaged as **`rapm.talent(end_year)`**:
  - **Step 1 — multi-season pooled RAPM (VALIDATED + adversarially verified).** `rapm.pool_rapm`
    pools a trailing 3-season window with recency decay (0.75) into one ridge. Predicts next-season
    net **better than single-season in 6/6 folds: mean corr 0.289 → 0.362, +0.074** (fair
    common-player-set comparison); lifts
    corr(net, TOI) ~0.00 → ~0.10 (stars rank higher, fewer linemate/depth spikes); robust to window
    (window=2 also wins). **Opposite of the NBA result** (multi-season RAPM rejected there) —
    hockey's single-season RAPM is noisier, so pooling adds real information. A 3-lens adversarial
    audit (`scripts/nhl_stage3_pool_report.py`) returned *sound* on all three, incl. the key
    refutation: it is **not "just more shrinkage"** — single-season predictiveness *falls*
    monotonically as ridge alpha rises (best ~0.339 at any alpha, still 0.04 below pooled 0.381), so
    the edge is genuine multi-season signal (2.3× possessions, averaged over changing linemates), not
    smoothing.
  - **Step 2 — box-informed OFFENSIVE prior (VALIDATED).** A skater's individual xG (own shots,
    MoneyPuck `I_F_xGoals`, 5v5) is largely linemate-independent, so blending it into the pooled
    offense (weight 0.4; `rapm.blend_box_offense`, `scripts/nhl_stage3_boxprior_report.py`) adds
    **+0.016 net corr in 6/6 folds** (0.359 → 0.375), most for thin-sample players. **Offense-only** —
    hockey has no comparable individual defensive box stat. NEGATIVE variant recorded: a box *net*
    prior from on-ice xG-differential does NOT help (it's a cruder, linemate-biased restatement of
    what RAPM already controls for, and hurts thin samples) — only the *individual-offense* signal is
    complementary.
  - **Cumulative (steps 1-2):** single 0.289 → pooled 0.362 (+0.074) → +box offensive prior (+0.016),
    every step 6/6 folds (~+0.09, ~30% relative).
  - **Step 3 — aging curves (measured).** `nhl/aging.py` + `scripts/nhl_stage3_aging_report.py`, on
    player birthdates (`scripts/nhl_fetch_birthdates.py` → NHL landing bio; MoneyPuck has none).
    Delta method: for every skater with single-season RAPM in consecutive years, the TOI-weighted
    change in off/def by age, smoothed (degree-2 polynomial on the deltas) and integrated to a level
    curve. Face-valid: **net peaks ~24, offense ~24** (gentle decline after), **defense declines from
    the youngest age** (mobility/skating-based xG suppression fades early — the hockey analog of the
    NBA's "blocks decline from the start"); net decline accelerates after ~30. Peaks younger than the
    NBA (~26-27), as hockey aging studies find. **Survivorship caveat:** only players in both seasons
    contribute, so the old-age fall-off is *understated*. The smoothed per-year `sdelta(age)` is the
    aging adjustment the projection applies.
  - **Step 4 — forward projection (`nhl/projection.py`, `scripts/nhl_stage3_projection_report.py`).**
    `project(end_year)` = per-skill **`beta · (talent + aging)`** → projected next-season off/def/net.
    Two measured findings: (a) **aging is one-year-neutral** for prediction (+0.002 corr) — kept
    because it is structurally correct (veterans should decline) but honestly small, echoing the NBA
    "directionally real, practically useless" aging results; (b) **persistence differs sharply by
    skill — offense beta ~0.62, defense beta ~0.31** (TOI-weighted, 6 folds): defensive RAPM is about
    half as persistent, the quantified NHL analog of "defense is the weakest, least-predictive
    metric," so defense is regressed hard toward the mean. **Validated:** actual-on-projected
    calibration slope averages **1.02** (6 folds), correlation 0.387 (= talent's), and the projection
    is correctly narrower than realized single-season net (SD 0.14 vs 0.39). Caveats: the projection
    is offense-dominated (defense near-zeroed by its low beta) and a few thin-sample young players
    leak high (residual pooled-RAPM linemate noise) — both wash out under minute-weighted team
    aggregation. `rapm.pool_rapm` now caches. **Next: Stage 4** — aggregate projected skaters + goalie
    + special teams → team goals-for/against, then Stage 5 season simulation → points distribution.
- ✅ **Stage 3b — HONEST (non-leaky) roster + TOI (`nhl/rosters.py`, `scripts/nhl_stage3b_honest_gate.py`).**
  Stage 4/5's end-to-end projection was a **leaky upper bound**: `aggregate.player_toi(Y)` reads season
  Y's ACTUAL MoneyPuck 5v5 rows, so it knew each team's full-season roster (February trade-deadline
  acquisitions included) AND every skater's realized minutes — neither knowable when a pre-season
  projection is made. Stage 3b supplies the point-in-time replacements, mirroring the NBA project's
  `roster_opening_day` + prior-minutes approach, then re-runs the exact same gate.
  - **Opening-day roster reconstruction (`rosters.opening_roster`).** No true pre-season roster feed
    exists this far back, so we reconstruct each team's opening roster from the **shift charts' first-
    appearance ordering** (as the NBA project reconstructs from first games). A skater is on team T's
    opening roster iff his **season debut was for T** AND fell within **T's first `k_games` (=20)**.
    That one rule handles trades correctly by construction: a deadline acquisition debuts for his OLD
    team, so he is excluded from the new team (and kept on the old one — right for opening day); a
    player dealt AWAY debuted for T and is kept. **Validated on 2023-24: Jake Guentzel (PIT→CAR at the
    deadline) lands on PIT's opening roster, never CAR's.** Roster sizes are a sensible ~23 skaters;
    TOI coverage of a team's actual 5v5 minutes plateaus by ~16-20 games at **~0.89**, matching the
    leaky bound's ~0.88 (the honest roster captures essentially the same minute mass, without the
    future knowledge). `k_games` is not a knife-edge — the debut rule does the trade-exclusion; the
    window only bounds mid-season call-ups/debuts.
  - **Projected TOI (`rosters.projected_toi`).** Each roster skater is weighted by his **prior-season
    (Y-1) total 5v5 icetime**, not his realized season-Y minutes; the ~6-7% with no prior season
    (rookies/new) get a bottom-rotation prior (`ROOKIE_TOI_SEC` = 500 min). Only relative weights
    matter (the aggregation is a minute-weighted mean).
  - **Wiring (`rosters.honest_toi` + `aggregate.team_ratings(..., toi=)`).** `honest_toi(Y)` joins the
    two into the `(player_id, team, icetime)` frame the aggregation consumes through a new `toi=` hook,
    so the honest projection reuses the EXACT same minute-weighted aggregation, replacement-level fill,
    and net→points calibration; only the roster set and minute weights change. Default (`toi=None`) is
    still the leaky bound, so the Stage 4 reports are unchanged.
  - **Result — the honest projection lands AT the bar, and the leak was worth ~nothing.** Over **9
    full-season folds (2015-16..2025-26, excl. the 2 covid seasons)**, honest + one-year carryover =
    **10.61 points** MAE, vs the walk-forward naive bar **10.83 on the same folds (−0.22, within noise
    ±0.24)** and the fixed Stage 1 bar 10.54 (+0.07). Critically, the **leaky bound over the same 9
    folds is 10.72** — so removing the roster/TOI leak costs **−0.11 (honest actually edges leaky)**,
    not the +0.35 the 3-fold snapshot suggested. **The earlier "leaky 10.10" was a favorable 3-fold
    subset (2023-25);** measured honestly over 9 folds, the leak was not inflating the number, which is
    the real validation: the point-in-time roster reproduces the leaky projection. The carryover still
    does the heavy lifting (honest plain 11.22 → +carry 10.61), rho ≈ 0.37-0.48 (unchanged), and the
    projection stays face-valid (2025-26 top CAR/VGK/EDM/DAL/TBL/LAK, bottom SEA/CHI/SJS/ANA/DET).
    **Honest read:** the model now sits *at* the bar with no leak and 9 folds instead of 3 — decisively
    *clearing* it awaits the still-missing Stage 5 levers (special teams, goalie level via
    goal-diff/Pythagorean, and the regulation/OT/shootout season simulation), exactly as the NBA
    project's first cuts sat at the bar until its later pieces landed.
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
- 🟡 **Stage 6 — the live projection + market comparison + NHL "Records" page (in progress).** The
  upcoming-season deliverable: a live projected-standings page and the downstream market comparison.
  - **✅ Live projection (`nhl/season.py`, `rosters.live_roster`/`live_toi`,
    `scripts/nhl_project_current.py`).** `nhl/season.py` is the reusable Stage 5/6 pipeline (panel,
    strength→goals calibration, season-sim mean, one-year carryover, interval sigma) -- **verified to
    reproduce the gate's 10.50 exactly**, so backtest and production cannot drift. The UPCOMING season
    has no shift charts, so the roster comes from the NHL web API (`ingest.roster`, the hockey analog
    of the NBA `commonteamroster` snapshot), weighted by each skater's prior-season 5v5 TOI. The
    script runs the shipped pipeline on the current roster → per-team 2026-27 projected points + a
    calibrated 80% interval, recording the roster snapshot date → `data/nhl/processed/
    projection_current.json`. Face-valid (COL/CAR/TBL/MIN top, VAN/CGY/CHI bottom; the carryover
    carries the extremes -- COL +11 after a 121-pt season, VAN −13 after collapsing to 58); the league
    points sum ~2934 matches the structural ~2926. Also outputs projected **wins** (the sim's W), so
    the bundle is comparison-ready for a wins-settled market.
  - **✅ Market feed (`nhl/market_live.py`), downstream-only.** Kalshi **`KXNHLWINS`** -- the hockey
    analog of the NBA's `KXNBAWINS` -- per-team season **win** totals as a threshold ladder,
    reconstructed to an implied win distribution (median / mean / p10 / p90) with the identical proven
    NBA algorithm. NOTE the target: Kalshi settles on **wins**, not standings **points** (a win is 2
    points but an OT/SO loss is still 1), so the comparison is to projected *wins*, never points. As of
    the 2026-27 pre-season the series has **no open events** (win markets post closer to opening night
    -- the NHL echo of bbref's Vegas 404 for the NBA), so `market_win_table` returns `{}` and the page
    shows no ring; it attaches automatically once the market opens.
  - **✅ The NHL "Records" page (`ui/nhl_records/`, `scripts/nhl_build_records_ui.py`).** A
    self-contained page following the shared UI conventions (design tokens, the grouped nav with a new
    **NHL → Records** link added to *every* page, the shared `wincurve-theme` toggle, Vercel route
    `/nhl/records`): 32 teams on a **shared points axis** with the projected mean + 80% interval band,
    sortable by points / offense / defense / carryover, each row expanding to its off/def/carryover
    drivers and a plain-English disagreement story. The market ring is absent-but-ready (an honest "not
    posted yet" note). Consumes `projection_current.json` inlined via a `__DATA__` placeholder. This is
    the per-team disagreement surface the whole project builds toward, now for hockey.
  - **⬜ Remaining.** The market comparison goes live when `KXNHLWINS` posts (activate the wins ring;
    reconcile the wins-vs-points axis); a historical track-record view awaits a free source of
    historical NHL market lines (none identified -- bbref has no NHL); per-team player-level roster
    detail + a what-if editor on the Records page; and the injury / known-absence overlays the NBA
    project carries.

---

## Conventions (same discipline as the NBA project)

- Walk-forward everywhere; any coefficient refit per fold on prior seasons only.
- Point-in-time correctness: features for season N use only pre-N information.
- Normalize per-60-minutes and z-score within season (the game changed: scoring, pace,
  goalie style).
- Every stage must beat mean-reverted previous points walk-forward, reported explicitly.
- Join on `franchise_id`; fail loud on data joins.
- **Market prices are strictly downstream — never a feature** (keeps the disagreement
  analysis meaningful).

## Data sources

- **NHL stats API** (`api.nhle.com/stats/rest/en`) — team summaries, shift charts. Free.
- **NHL web API** (`api-web.nhle.com/v1`) — season index, rosters, play-by-play. Free.
- **MoneyPuck** (`moneypuck.com`) — shot-level and season xG (team/skater/goalie), 2007-08+.
  Free, no key.
- **Optional paid (validation only, never an input):** Evolving-Hockey publishes a ready-made
  GAR/WAR (goals/wins above replacement) that we would use as an occasional downstream
  cross-check, the way the NBA project cross-checks against Basketball-Reference Win Shares.
- Market lines (season points over/under; Stanley Cup / division / playoff odds) — sourced in
  Stage 6; downstream comparison only.
