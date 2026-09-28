# NHL — data layer, baselines, impact metric, and honest roster (Stages 0–3b)

> Moved verbatim from `nhl/DESIGN.md` on 2026-09-28 so it is read on demand. The current
> status, shipped model, traps, and open items live in `nhl/DESIGN.md`; "above" / "below"
> may now point to a sibling file in `docs/nhl/`.

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
