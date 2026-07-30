# wincurve — NBA Season Record Projection System

## What this project is

Projects each NBA team's regular-season record for an upcoming season as a
**probability distribution** (not a single number), built bottom-up from the current
roster's players. Compares those projections against betting and prediction-market
prices (Kalshi, Polymarket, historical sportsbook win totals) to find places where an
analytically-driven model disagrees with the market.

**This is a research tool for identifying market disagreements. The user is not
betting.** The deliverable is *explainable per-team disagreement* — "we differ from
the market on this team, and here is the structural reason why."

Current projection target: **2026-27 season**. Shipped backtest MAE (roster mode + carryover +
RAPM defensive blend): **7.77 wins** vs market 6.88 (was 7.95 before the RAPM def-blend).

See [DESIGN.md](DESIGN.md) for full architecture, statistical traps, and staged plan.

---

## Communication preferences (IMPORTANT)

**Always expand statistical acronyms on first use in any response, with a one-line
plain-English description.** The user has not taken a statistics class recently and
has explicitly asked for this. Do not assume familiarity with standard notation.

Examples of the expected style:
- MAE (mean absolute error — the average miss, ignoring whether it's high or low)
- RMSE (root mean squared error — like MAE but punishes big misses harder)
- SD (standard deviation — the typical distance from the average)
- R² (r-squared — the share of variation the model explains, 0 to 1)

This applies to output printed by scripts too, not just chat responses. Reports
should carry their own legend.

---

## Decisions made (and why)

| Decision | Choice | Rationale |
|---|---|---|
| Player impact metric | **Compute our own** from nba_api | Guarantees full 21-season point-in-time coverage, no licensing constraints, we control era normalization. Full RAPM-from-play-by-play kept as documented upgrade path. |
| Fit / coaching modules | **Strict backtest gate, but report findings either way** | Keeps the production projection trustworthy; still answers the user's curiosity. A module that fails its gate gets dropped from the projection and written up as a finding. |
| Timing scope | **Preseason projection only** | Matches how win-total markets are priced; keeps backtest protocol clean. No in-season updating. |
| Modeling target | **Win percentage**, not wins | Three seasons in the window are not 82 games. Wins are a presentation-layer conversion. |
| Franchise join key | **`TEAM_ID`** | Stable across relocations and renames (Seattle→OKC, New Jersey→Brooklyn, Bobcats→Hornets). Never join on name or abbreviation. |
| Market data role | **Strictly downstream, never a feature** | If market prices touch training, the disagreement analysis becomes circular and meaningless. |

---

## Architecture

```
A. Player talent projection   (impact per 100 possessions, aging, shrinkage; off/def decoupled)
B. Minutes & availability     (240 min/game budget, injuries, replacement level)
C. Team aggregation           (minute-weighted sum -> separate off and def ratings)
D. One-year carryover         (+ rho * last-season residual; error persists ~1 season)
E. Monte Carlo season sim     (real schedule, home court, rest -> win distribution)
F. Market comparison          (downstream only)
```

**Offense and defense are decoupled** (shipped 2026-07, `decouple=True` in
`project_team_ratings`): each player's offense and defense is projected and aged separately and
calibrated against team offense/defense with its own slope, net = off + def. It is MAE-neutral
(gate: `scripts/gate_decouple.py`, 7.960 -> 7.957 excl. shortened folds, coverage 80.0 -> 80.7%)
but lets the app attribute a rating to offense vs defense and surface the defensive weakness.

Layer A's defensive component is the model's **weakest link**, and now we can say exactly why:
the box-score defensive metric is ~60% defensive-rebounds-plus-blocks by fitted weight (rebounds
alone 41%), so `def_impact` correlates 0.82 with defensive rebounding — it largely measures
*being a center*. Elite perimeter defense (containment, deterrence) produces few countable
events, so the metric's top-12 are ~10 centers and it rates celebrated stoppers (Dort, NAW,
Anunoby, Holiday) near zero. Defense R² is 0.33 vs offense 0.82. Box-informed RAPM from
play-by-play is the structural fix; it is surfaced per-player in the UI (box-vs-RAPM "D↑/D↓"
flags) but not yet in the projection — see the RAPM section for why (the carryover substitutes
for its aggregate value).

**Why bottom-up:** 21 seasons x 30 teams = **630 team-seasons**. That is far too few
rows to fit a team-level model with the many features originally envisioned (positional
balance, pace, coaching, turnover type, fit). All heavy lifting therefore happens at the
**player level** (~10,600 player-seasons), and layer C stays nearly parameter-free.
Minute-weighted sums of player impact predict team rating well enough to make this work.

**On "fit":** it is *not* a feature set to add. Player impact metrics already contain
rebounding, turnovers, and shooting — re-adding them double-counts. Fit is modeled as
the **residual that additive aggregation fails to explain**, reduced to ~5-8
diminishing/increasing-returns parameters (see DESIGN.md §3). That is a size 630 rows
can actually support.

---

## Measured results so far

Walk-forward means: to predict season N, train only on seasons before N. All errors in
82-game-equivalent wins, lower is better.

**Current shipped model** (roster mode + one-year carryover), 2017-18..2025-26:

| Model / baseline | MAE | Notes |
|---|---|---|
| Market (preseason win totals) | **6.88** | the yardstick; we do not beat it |
| **wincurve — roster + carryover + RAPM def-blend** | **7.77** | shipped; all 9/9 folds beat the box-only model |
| wincurve — roster mode + carryover (box def only) | 7.95 | the prior shipped model, before the RAPM blend |
| Leaky upper bound (actual roster + minutes) | 7.11 | ceiling given perfect roster knowledge |
| Mean-reverted previous wins ← *the gate* | 8.13 | every stage must clear this |
| Previous wins (persistence) | 8.75 | |
| Always .500 | 10.18 | |
| *Theoretical binomial noise floor* | *3.44* | not achievable (see below) |

Longer-window baseline figures (2013-14..2025-26): market 6.67 excl. 2019-20, naive 8.07.
Fitted mean-reversion coefficient **k ≈ 0.62**, stable across folds: teams retain ~62% of
their distance from .500 year over year. Implied true-talent spread is **SD ≈ 11.6 wins**
vs **4.3 wins** of luck, so talent variation dwarfs randomness and projection is worth doing.

**The honest read:** we do not beat the market on aggregate accuracy, and should not expect
to. The deliverable is per-team *disagreement* with an attached explanation, plus calibrated
intervals that widen for genuinely uncertain (high-turnover) teams.

### ⚠️ How to read the noise floor honestly

The 3.44 MAE binomial floor is **not an achievable target**. It assumes we know each
team's true strength in October *and* that strength stays constant all season. Real
rosters change through unforeseeable injuries and trades, so the practically achievable
frontier is considerably higher — and the market's 6.67 is probably close to it.

Do **not** present "market captures only 30% of the gap to the floor" as an opportunity
estimate. Realistic aggregate improvement over the market is on the order of a few
tenths to ~1 win of MAE, and may be zero. The genuine value of this project is
per-team disagreement attribution, not beating the market on average.

---

## Data inventory (all availability windows empirically verified)

Stored in `data/` (gitignored; regenerate with `python scripts/fetch_all.py`).

| Dataset | Rows | Seasons | Span |
|---|---|---|---|
| `player_advanced` | 10,595 | 21 | 2005-06 → 2025-26 |
| `player_base` (per-100) | 10,595 | 21 | 2005-06 → 2025-26 |
| `team_advanced` | 630 | 21 | 2005-06 → 2025-26 |
| `game_log` (team-games) | 50,538 | 21 | 2005-06 → 2025-26 |
| `rim_defense` (tracking) | 6,823 | 13 | 2013-14 → 2025-26 |
| `hustle` | 5,455 | 10 | 2016-17 → 2025-26 |
| preseason win totals | 630 | 21 | 2005-06 → 2025-26 |

**Availability cliffs that constrain the fit work:** tracking data starts 2013-14 and
hustle data 2016-17. So "fit" features exist for only 10-13 seasons (300-390
team-seasons), roughly half the backbone's coverage.

### Source notes

- **nba_api** (stats.nba.com) — primary. Rate-limited; every call goes through
  `nbaproj/cache.py` (disk cache + throttle + exponential backoff). Re-runs are free.
- **Basketball-Reference** — preseason win totals only, from
  `/leagues/NBA_<end_year>_preseason_odds.html`. `/leagues/` **is allowed** by their
  robots.txt; `*/gamelog/`, `*/splits/`, `*/on-off/`, `*/lineups/`, `*/shooting/` and
  `/basketball/` are **disallowed** and must not be scraped — get that data from
  nba_api instead. Honor `Crawl-delay: 3`. Requires a browser User-Agent.
- **Kalshi** — `api.elections.kalshi.com/trade-api/v2`, reads fine unauthenticated.
- **Polymarket** — `gamma-api.polymarket.com`, reads fine unauthenticated.
  Both are too recent for historical baselines; they are for the *live* 2026-27
  comparison only.
- **Pro Sports Transactions** (historical injuries) — returns 403 without a normal
  User-Agent. Best historical injury-reason source; fallback is deriving games-missed
  from box-score absences via nba_api (ToS-clean but loses injury *reason*).

### Season-length landmines (handled, do not regress)

- 2011-12 = 66 games (lockout); **win totals were set for 66 games** (league avg 33.1)
- 2020-21 = 72 games; **totals set for 72 games** (league avg 36.0)
- 2019-20 = 64–75 games, varying **by team** (only 22 of 30 went to the bubble);
  **totals were set for a full 82** because the interruption came later. This makes its
  market comparison apples-to-oranges — reported separately, never silently mixed in.
- 2012-13 = 81 games for BOS and IND (game cancelled after the Boston Marathon bombing)

---

## UI

**Multi-season app.** The UI now shows the last 5 completed seasons (2021-22..2025-26) plus
the upcoming one, via a season selector. Historical seasons are walk-forward hindsight
projections (only pre-season data) with the ACTUAL result shown as a diamond marker and the
season's MAE in the header -- a visible backtest. Bundles built by `scripts/build_snapshots.py`
into `data/processed/snapshots.json` (~700 KB, all seasons inlined); the what-if editor works
on every season and reproduces each baseline to rounding. Rebuild:

```
python scripts/project_current.py       # upcoming-season live bundle
python scripts/fetch_market.py --refresh # live Kalshi win totals (downstream comparison)
python scripts/build_snapshots.py       # historical bundles + combined snapshots.json
python ui/build.py                      # inline into ui/projections.html
```

**Live market comparison (upcoming season only).** Each 2026-27 team bar carries a hollow
blue ring marking the win total the prediction market implies **right now**, plus a
`mkt N · ±diff` readout where diff is *our projection minus the market*. Source is
**Kalshi `KXNBAWINS`** (`nbaproj/market_live.py`), the only live per-team win-total market:
bbref Vegas over/unders are still unposted this early (404), and Polymarket has no per-team
win-total market. Kalshi quotes a **threshold ladder** ("20+/25+/30+ wins"), so we
reconstruct a full market-implied distribution (median, mean, p10/p90) and compare
distribution-to-distribution, not just point-to-point. **Strictly downstream — the market
never touches the model** (that rule is what keeps the disagreement analysis meaningful).
Mean |ours − market| ≈ 4.2 wins; biggest gaps are the deliverable (open a team for the
roster reason). Refresh through the season with `fetch_market.py --refresh`.

**Explainability (added for the "is Impact WAR?" question).** A plain-English glossary
(`<details>` at the foot) defines every number. The player table now has an **≈ Wins**
column — the WAR-like (wins-above-replacement) translation of Impact, since **Impact itself
is a per-100-possession rate, not a win count**. The panel shows **Minutes supplied /240**,
which surfaces deep off-season rosters (see the roster-bloat investigation below — the
"bloat" was checked and is not a defect). The
green/red edit delta is now tooltip-labelled "change from the original projection".

**Offense/defense split + defensive disagreement (added 2026-07).** Every team row shows its
rating split as **`O ±x · D ±y`** (colored by sign), so you can see whether a projection is
carried or dragged by offense vs defense — e.g. Detroit is defense-carried (O −0.1, D +0.8),
Charlotte offense-carried (O +0.6, D −0.5). The roster panel splits Impact into **Off / Def**
columns and its header shows the team **Offense / Defense** rating. Players whose box-score
defense disagrees with play-by-play **RAPM** get a **`D↑` / `D↓` flag** by their name (`D↑` =
RAPM higher, we likely underrate his D — e.g. Alex Caruso, NAW; `D↓` = we likely overrate,
e.g. Dyson Daniels), hover for both numbers. RAPM is comparison-only, not in the projection.
This is fed by the decoupled projection: `project_current.py` / `build_snapshots.py` emit
per-team `off_rating`/`def_rating`, per-player `off`/`def`, and each player's most-recent
`box_def`/`rapm_def` (via `nbaproj.rapm.box_vs_rapm_by_player`, walk-forward). The client
recompute is decoupled too, so a roster edit reprices offense and defense independently.


`ui/template.html` + `ui/build.py` -> `ui/projections.html` (self-contained, data inlined).
Rebuild after regenerating projections:

```
python scripts/project_current.py && python ui/build.py
```

Design decisions: 30 team rows on a **shared win axis**, so range widths are directly
comparable across teams — that comparability is the whole point, since range width carries
information. Uncertainty is encoded twice, in bar width and in hue (teal = tight, amber =
high turnover). Monospace for all data, system sans for prose.

Roster edits recompute in the browser, and this is exact rather than approximate: the
aggregation is a minute-weighted mean, so it can be reproduced client-side. A precomputed
per-team grid of simulated win distributions across rating offsets is interpolated, which
keeps real schedule strength intact without running a simulation in the browser.

**Caveats are stated in the page itself, deliberately:** that the model does not beat the
market on aggregate accuracy (7.77 vs 6.88 MAE), that single-player what-ifs extrapolate the
calibration slope further than the backtest validated, and (upcoming season) that the market
ring is shown-only and never an input. Do not remove them.

**Deployment: Vercel, private repo, `ui/` only.** GitHub Pages free requires a public repo;
Vercel Hobby deploys the private repo and still serves a public URL. The site is configured
to serve **only `ui/`** (Vercel *Root Directory* = `ui`), so `data/`, `nbaproj/`, and
`scripts/` are never uploaded or reachable. `ui/vercel.json` serves `projections.html` at
`/`; `ui/.vercelignore` keeps `build.py`/`template.html` out. Full steps in
[ui/DEPLOY.md](ui/DEPLOY.md). The self-contained `projections.html` already inlines its data,
so nothing beyond the app's own numbers is exposed.

## Layout

```
nbaproj/
  cache.py        throttled, retrying, disk-cached nba_api wrapper
  ingest.py       per-dataset pulls; availability windows as constants
  teams.py        team-season target table; franchise spine
  baselines.py    walk-forward naive baselines + noise floor
  odds.py         historical preseason win totals scraper + strict franchise join
  market_live.py  LIVE Kalshi win-total ladders -> implied distribution (downstream only)
scripts/
  fetch_all.py       full historical pull (idempotent, resumable)
  baseline_report.py Stage 1 report: the bar
  fetch_market.py    pull live Kalshi lines -> data/processed/market_2026_27.json
```

---

## Conventions

- Walk-forward backtesting throughout. Any coefficient is refit **per fold** using only
  prior seasons. Never fit on the full history and report it as out-of-sample.
- **Point-in-time correctness:** features for season N may use only information
  available before season N started. This is the easiest way to fake a good backtest.
- Normalize per-100-possessions and z-score **within season** — the league changed
  enormously across the window (pace ~90 → ~100 possessions, three-point attempts
  ~16 → ~38 per game).
- Every new stage must beat mean-reverted previous wins on walk-forward MAE, or it does
  not ship. Report the comparison explicitly.
- Fail loudly on data joins. `odds.py` raises if any season fails to match all 30
  teams; prefer that to a silent partial join.

---

## Roadmap

- ✅ **Stage 0** — Data layer: cached, throttled, point-in-time-correct pulls, 21 seasons
- ✅ **Stage 1** — Baselines: naive + market + noise floor. **Bar = 8.07 MAE naive,
  6.67 MAE market**
- ✅ **Stage 2** — Player impact metric, aging curves, shrinkage. Projection beats
  "reuse last season" by **9.4%** (MAE 1.260 → 1.142 points per 100 possessions),
  improving monotonically across all 10 walk-forward folds.
  **Known limitation:** offense calibrates well (r-squared 0.88), defense does not
  (0.45) and its player-level values are close to a restatement of rebounding
  (corr 0.89) — it partly measures *being a centre*. Aggregates acceptably at team
  level (net-rating r-squared 0.84) since every team plays centres ~48 min/game.
  **Known defect:** aggregation slope is 7.7 where algebra predicts 5, because
  sub-threshold players get no estimate — replacement level in Stage 3 is the fix.
- 🟡 **Stage 3** — Availability model **validated**; roster/rookie/override machinery
  **built**; minute-allocation accuracy cannot be fully validated until Stage 4 ties it
  to team outcomes.
  - Availability: MAE 0.186 vs 0.205 for "reuse last season" and 0.219 for league
    average. Still ~15 games of error per player-season — injuries are largely
    irreducible, as expected.
  - Replacement level = **-1.45** points per 100 possessions (average of 250-750
    minute players).
  - Rookie priors by draft bucket: top-3 picks 92% play, ~1,677 minutes, impact -0.10;
    picks 31-60 71% play, ~331 minutes, impact -4.06. All rookie impacts are negative,
    which is correct.
- ✅ **Stage 4** — Team aggregation built, minute budget enforced, calibration fitted
  per fold on *projected* aggregates.
- ✅ **Stage 5** — Monte Carlo simulation shipped; intervals **calibrated** (nominal 80%
  → 79.6% coverage) after the recency-weighted sigma fix and two simulation bug fixes
  (residual-margin denominator; neutral-site games). Two roster-knowledge variants are
  reported so leakage is visible; the honest preseason number is **roster mode = 8.34**,
  cut to **7.95 by the carryover** below.
- ✅ **Stage 5b — one-year residual carryover** (`nbaproj/carryover.py`). The team-rating
  error persists ~1 season; adding rho·(last-season residual) improves roster-mode MAE
  **8.30 → 7.95 (+0.35 wins)** and lifts coverage 77% → 80%. Suppressed after shortened
  prior seasons. **The one change this project made that both beat the backtest gate and
  survived adversarial verification.**
- ✅ **Stage 6 — "fit" as residual structure: tested, REJECTED.** Depth/concentration
  features are null out-of-sample once team quality is controlled, and their sign is
  *opposite* the folk hypothesis. Diminishing-returns curves not worth carrying. See the
  negative-results table. (The residual *does* have structure — one-year memory — captured
  by the carryover, which is not a "fit" term.)
- 🟡 **Stage 7 — coaching / continuity.** Minute-concentration is a real coach trait
  (intraclass 0.45) but using it hurt the backtest; roster-continuity persistence is real
  but the interaction it implies is not significant. **Blocked on data:** `team_coaches`
  is mis-dated by one season at changes — re-pull before further coaching work.
- ✅ **Stage 8 — market comparison.** Historical market baseline done (bbref preseason
  win totals). **Live 2026-27 Kalshi pull shipped** (`nbaproj/market_live.py`,
  `scripts/fetch_market.py`): all 30 teams' threshold ladders reconstructed into implied
  win distributions and shown beside ours in the UI (hollow ring + `mkt N · ±diff`). Vegas
  (bbref) not yet posted (404); Polymarket has no per-team win-total market. Still to do:
  the contract-year hypothesis test. **Downstream-only, never a feature.**
- ✅ **Offense/defense decouple** — offense and defense projected, aged, and calibrated
  separately (`decouple=True`); MAE-neutral (`scripts/gate_decouple.py`: 7.960 → 7.957 excl.
  short folds, coverage 80.0 → 80.7%). Shipped because it unlocks the per-team off/def split
  and the per-player defensive-disagreement surface in the UI.
- ✅ **RAPM defensive blend — SHIPPED into the projection.** RAPM now covers **2013–2025**.
  The blanket swap was MAE-neutral (the carryover substitutes for it on stable rosters), but
  once defense is *decoupled* and given its own calibration, blending the box and RAPM
  defensive team aggregates by **roster turnover** (`agg_def = (1−w)·box + w·rapm`, `w` = new-
  minute share) clears the gate decisively: **7.96 → 7.77 excl-short, all 9/9 folds improved,
  coverage 80 → 81%** (`scripts/gate_rapm_blend.py`, `nbaproj.rapm_blend`). It lives in the one
  decoupled pipeline — no second arm — since a steady roster's defense is already handled by
  box + carryover while RAPM's player-level value follows the churn the carryover can't. Pure
  RAPM def also helps now (+0.14); the blend beats both. The UI recompute and the `D↑/D↓` flags
  use it too. The 2025-26 live blocker was resolved by the PlayByPlayV3 reconstruction
  (`nbaproj.bulk_pbp`, corr 0.99). See the RAPM section below.

### Offseason movement & absences — how each is handled

| Factor | Status | Mechanism |
|---|---|---|
| Trades, free agency | ✅ | `commonteamroster` live snapshot (2026-27 already posted) |
| Retirements | ✅ | Retired players are simply absent from rosters |
| First-round picks / rookies | ✅ | Draft-position priors in `nbaproj/rosters.py` |
| Injury/suspension **risk** | ✅ | Availability model from games-missed history + age |
| **Specific known absences** | ⚠️ manual | `data/overrides/known_absences.json` — no free feed exists |

**A projection is only as current as its snapshot date.** Trades continue all season; a
July projection cannot know about a November deal. Always record the snapshot date with
any output. Historical injury *reasons* are unavailable — Pro Sports Transactions is
behind a Cloudflare bot challenge we will not bypass — so absences mix injury with
rest, suspension and coach's decision. Hence the name "availability", not "health".

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

### 🟡 Defensive metric / RAPM — pipeline + estimator + integration test all done; not shipped

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

### ✅ 2025-26 play-by-play via PlayByPlayV3 (`nbaproj.bulk_pbp.build_segments_bulk_v3`)

The bulk mirror (shufinskiy/nba_data) stopped shipping the v2 `nbastats` feed for recent
seasons — 2025-26 exists only as **`nbastatsv3`** (PlayByPlayV3), a different schema with no
shared columns. `segments_for_season` now auto-routes: v2 where available, else v3. The v3
reconstruction handles its quirks — a sub names only the outgoing player by id, the incoming by
name ("SUB: in FOR out"), resolved from a per-game map of `playerName` + `playerNameI` (the
initial+last form used on same-team last-name collisions) + outgoing names from other subs, all
diacritic-normalised; the rare unresolved incoming gets a placeholder id. Attributing points in
event order (not by timestamp) is what makes the offense/defense split reproduce, not just the
net. **Validated on 2024 (both schemas exist): corr 0.99 total, 0.98 offense, 0.98 defense** vs
the v2 reconstruction. Any season 1996-97+ is now reachable regardless of which schema the
mirror ships.
RAPM stays a **documented, validated candidate**. The turnover-weighted blend is the natural
next step once 2025-26 lands and if it clears a coverage check.

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

### Decisions pending user input

- ⬜ **Defense estimation.** *(Deferred by user: finish the pipeline first, revisit
  if defense proves to be the binding constraint.)* Box-score defense is positionally biased and cannot be
  fixed by adding more box features. The real fix is RAPM (regularized adjusted
  plus/minus) from play-by-play: ~25,000 games to pull and cache, plus ridge
  regression on stint-level data. Roughly an overnight data pull. Worth it only if
  defensive *personnel* questions matter to the user beyond aggregate team projection.

### Open items

- ⬜ Historical **injury reasons** still unsourced (Pro Sports Transactions needs a UA;
  otherwise only games-missed is available)
- ✅ Live 2026-27 market comparison shipped via **Kalshi** (`market_live.py`). bbref Vegas
  still 404; Polymarket has no per-team win-total market. Re-check bbref later for the Vegas
  over/under (would add a second live line).
- ✅ **"Bloated summer rosters" investigated and rejected** — not a defect; trimming fails
  the gate and the flagged gaps are real (defensive-metric) disagreements. See the
  INVESTIGATED & RESOLVED note and the negative-results table.
- ⬜ Contract/salary history unsourced (only needed for the contract-year test)
- ⬜ Roster definition for backtest: plan is to reconstruct opening-night rosters from
  each season's first games. Using full-season rosters would understate real-world
  error, since February's roster is unknown in October.

---

## Hypotheses to test (not assumed)

- **Contract-year / post-extension effects.** Popularly believed, poor replication
  record. Major confound: players get paid *after* a career-best season, so apparent
  post-contract decline is substantially regression to the mean. Test explicitly;
  do not assume into the model.
- **Peak age.** Folk wisdom says 28-30; evidence points to ~26-27 for overall impact,
  with three-point shooting holding later (~29-31) and rim finishing / defensive
  mobility declining earlier. Model aging **per skill**, not as one curve.
- **Health history × age interaction.** Plausible that injury history is more damaging
  for older players. Recent games-missed likely carries more signal than career total.
- **Coaching.** Only ~10-30 team-seasons per coach and heavily confounded with roster
  quality. Roster continuity is better documented and far easier to measure — try it
  first.
