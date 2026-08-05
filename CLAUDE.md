# wincurve — NBA Season Record Projection System

> **Multi-sport expansion (started 2026-08).** This repo is becoming a shared-core
> monorepo. The NBA system below is the mature reference implementation (package
> `nbaproj/`). **NHL** is the first expansion, under `nhl/` with its own
> [nhl/DESIGN.md](nhl/DESIGN.md) — **the authoritative NHL doc; read it first for NHL work.**
> Done so far: **Stage 0** (data layer), **Stage 1** (baselines — the bar is **10.54 MAE
> points**, mean-reverted previous points), and **Stage 2** (impact metric — skater xG-RAPM
> and goalie GSAx, both face-validated on 2023-24: MacKinnon #1 on offense, Hellebuyck #1 in
> goal — **plus a self-contained impact-viewer UI**, `ui/nhl/`, sortable/season-selectable
> skater xG-RAPM + goalie GSAx leaderboards; `scripts/nhl_build_impact_ui.py`).
> Sport-agnostic pieces are extracted into `core/` **incrementally**, only once a
> second sport proves the seam (currently just `core/httpcache.py`, the throttled disk-cached
> HTTP client). The NBA code is **not** refactored onto `core/` until the NHL build shows what
> is genuinely shared. NFL and the top-5 European soccer leagues are planned next. The
> communication and walk-forward conventions below apply to every sport.
>
> **▶ NHL pickup point (next session):** shift pull DONE (all 16 seasons 2010-11..2025-26; the
> `/shiftcharts` floor is 2010-11, not 2007-08 — empty before then); the impact **viewer ships all
> 16 seasons and deploys at `/nhl`** on Vercel. **Stage 3 (impact refinement) is COMPLETE — all 4
> steps done and validated:** multi-season pooled RAPM (`rapm.pool_rapm`, +0.074 next-season corr,
> 6/6 folds, adversarially verified as real signal not shrinkage) + box-informed *offensive* prior
> (`rapm.blend_box_offense`, +0.016, 6/6) → **`rapm.talent()`**; aging curves (`nhl/aging.py`, net
> peak ~24, defense declines early); and the **forward projection** (`nhl/projection.py`,
> `project(end_year)` = per-skill `beta·(talent+aging)`, offense β≈0.62 / defense β≈0.31, calibration
> slope 1.02). Steps 1-3 are on `main`; step 4 is on branch `nhl-stage3-projection`. **NEXT: Stage 4**
> — aggregate `project()`'s skaters + goalie GSAx + special teams into team goals-for/against rates
> (calibrated on projected aggregates), then **Stage 5** season sim (regulation/OT/shootout) → the
> points distribution. See the Roadmap in [nhl/DESIGN.md](nhl/DESIGN.md).

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
RAPM defensive blend + rim/hustle tracking defense + position-relative rebounding + BPM position
fallback + RAPM prior re-anchored on the current box defense): **7.58 wins** vs market 6.88 (7.95
before the RAPM def-blend; 7.77 before tracking defense; 7.74 before the position-relative-rebounding
fix — a real +0.11, both more accurate AND fairer to guards; 7.62 before the position-fallback fix
that extends it to the 8 seasons with no listed positions, +0.01; 7.61 before re-anchoring the RAPM
prior on the current box defense, a further +0.03).

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
| Timing scope | **Preseason projection is the shipped default** | Matches how win-total markets are priced; keeps the backtest protocol clean. A SEPARATE in-season / rest-of-season model (own gate) now also exists for mid-season re-runs — see the shipped in-season item in the roadmap. |
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
| **wincurve — + RAPM prior re-anchored on current box defense** | **7.58** | shipped; +0.036 (7.628 → 7.591 paired-seed, ±0.017 SE, 5/6 folds) — the RAPM prior was stale (pre position/tracking fixes) |
| wincurve — + BPM position fallback (pre-2013-14) | 7.61 | prior; +0.011 (7.622 → 7.612, ±0.0039 SE, 5/6 folds) — fixes position-relative rebounding's silent no-op on 8 of 21 backbone seasons |
| wincurve — + position-relative defensive rebounding | 7.62 | prior; +0.11 (7.74 → 7.62, ±0.055 SE, 6/9 folds) — first defensive change to help accuracy AND credibility |
| wincurve — + RAPM def-blend + rim/hustle tracking def | 7.74 | prior; tracking step within noise (7.77 → 7.74) — kept for per-player credibility |
| wincurve — roster + carryover + RAPM def-blend | 7.77 | before the tracking-defense features |
| wincurve — roster mode + carryover (box def only) | 7.95 | the shipped model before the RAPM blend |
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
| `player_passing` (tracking) | 6,942 | 13 | 2013-14 → 2025-26 |
| `matchups` (defensive, `MatchupsRollup`) | 20,022 | 9 | 2017-18 → 2025-26 |
| `player_awards` (All-NBA/All-Def) | 572 | 29 | 1997 → 2025 (honoree pool) |
| preseason win totals | 630 | 21 | 2005-06 → 2025-26 |

`rim_defense` + `hustle` now feed the **defensive** metric (`add_tracking_features`);
`player_passing` (potential/secondary assists, points created) is pulled for the offensive
creation experiment. `matchups` (who guarded whom, points/FG% allowed as primary defender) is
pulled and kept as a clean asset but its perimeter-containment feature was **rejected** — too noisy
even multi-season-pooled (see the negative-results table). Tracking is ~half the 21-season backbone,
so these features are league-average-filled before their first season.

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

**Multi-season app.** The UI shows the **7 full seasons in the backtest window** (2017-18, 2018-19,
2021-22..2025-26 — the two shortened seasons 2019-20/2020-21 are skipped) plus the upcoming one,
via a season selector. Historical seasons are walk-forward hindsight projections (only pre-season
data) with the ACTUAL result shown as a diamond marker and the season's MAE in the header -- a
visible backtest. (Older than 2016-17 is blocked on data: `team_rosters` for opening-day roster
reconstruction only exists 2016-17+, though Vegas lines go back to 2005-06 — see the Open-items
roadmap for the historical-roster pull that would unblock the rest.) Bundles built by `scripts/build_snapshots.py`
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

**Historical market comparison (completed seasons, shipped 2026-07-31).** Each completed
season's view *also* carries a market ring — the **preseason Vegas over/under** for that year
(bbref, `market_baseline.parquet`, joined by TEAM_ID in `build_snapshots._attach_historical_market`,
already 82-game-normalized). With our mean, the Vegas ring, and the actual diamond on one bar,
you read per team whether the model or the market landed closer. Historical is **Vegas-only** —
Kalshi/Polymarket are too recent to have any history. Marker/readout are source-aware
(`marketLabel` → "Kalshi" live, "Vegas" historical); the market is still strictly downstream.

**Track record view (shipped 2026-07-31, extended to 7 seasons 2026-08-02).** A `Projections /
Track record` toggle opens a second view that scores the model against the market longitudinally:
per completed season, our MAE vs the Vegas MAE vs a .500 baseline (bars), the honest aggregate
(**7 full seasons: model 7.75, Vegas 6.90, model closer in 2 of 7** — we do not beat a sharp
market on average, as promised), and the per-team **best calls / where the market won**
(biggest model-vs-Vegas disagreements ranked by
who was closer to the actual). All computed **client-side from the inlined snapshots**
(`renderTrackRecord`, `seasonModelMAE`/`seasonMarketMAE`/`seasonBaselineMAE`) — no new data.
The per-team disagreement payoff is the whole point of the tool, now made visible and gradeable.

**Disagreement attribution + conviction (shipped 2026-08-02).** The tool's core deliverable, in
each team's expanded panel (the "details" surface — the natural place to gate behind auth later).
Whenever a market line exists (Kalshi live / Vegas historical), a banner (`disagreementBlock`)
shows: the gap framed as *we project X · market implies Y · we are ±Z*, then **what drives our
number** — which of offense / defense / one-year-carryover leads (in points/100), plus the
roster's top pieces by ≈Wins — then a **conviction tag** (High / Medium / Low). Conviction is a
*research aid, not a calibrated probability*: it starts High and drops one level for each of
(a) the gap being **defense-led** (our least-trusted metric), (b) **roster turnover > 35%**, and
(c) the **RAPM-only arm disagreeing with the blend by > 2.5 wins** — so it flags exactly which
disagreements to distrust (e.g. BOS +9.9 → Medium, defense-led; ATL −10.8 → High, offense-led on
a stable roster; NYK −7.3 → Medium, RAPM disagrees). Within 1.5 wins it says "we broadly agree"
rather than manufacturing a story. All client-side from data already computed; no gate (it is an
explanation layer, not a projection input).

**Explainability (added for the "is Impact WAR?" question).** A plain-English glossary
(`<details>` at the foot) defines every number. The player table now has an **≈ Wins**
column — the WAR-like (wins-above-replacement) translation of a player's value, since **Impact
itself is a per-100-possession rate, not a win count**. It decomposes the *same* model the team
rating uses: offense and defense priced by their own slopes (defense's is lower, since defense
is less predictive) and defense blended toward RAPM by roster turnover — so a rebounding centre
the box score over-credits on defense (Drummond) is discounted rather than tied with a scoring
guard (Brunson). `winParts(p, team)` in the UI mirrors the team aggregation exactly — including
the **240-min/game budget cap** (`teamCapFactor`, fixed 2026-07): the aggregation scales an
over-budget summer roster down before minute-weighting, so ≈Wins applies the same factor or it
over-credits every player on a bloated roster by up to ~1.5×. With the cap, the per-player ≈Wins
sum **reconciles exactly** to the team's rating-above-replacement (verified 0.000 max error over
all 30 teams; before the fix ATL/CHA/MIL overstated by 3–6 wins in aggregate). The panel shows
**Minutes supplied /240**, which surfaces deep off-season rosters (see the roster-bloat
investigation below — the "bloat" was checked and is not a defect). The green/red edit delta is
now tooltip-labelled "change from the original projection".

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

**RAPM-only defensive display arm (added 2026-07).** Beside the shipped projection, every team
carries a **second win projection with defense priced purely from play-by-play RAPM** instead of
the box+RAPM turnover blend (same offense, same carryover). It shows as a rose ▼ marker on the
bar and a **`rapm N · ±diff`** readout (diff = RAPM-only − blend; the panel header shows
`RAPM-only D ±y → N wins`). The gap isolates the defensive-metric choice and is a
defensive-uncertainty signal: it is largest on **low-turnover** teams — where the blend leans on
the box metric that misses perimeter defense — and near-zero on high-turnover ones (where the
blend already leans on RAPM). Poster child: **New York** (10% turnover, blend 44 → RAPM-only 51,
+6.9), a roster of perimeter defenders the box underrates. Emitted per-team as `rating_rapm` /
`def_rating_rapm` / `wins_rapm` with meta `def_slope_rapm`/`def_intercept_rapm` (RAPM's own,
lower ~3.3 defensive slope, calibrated walk-forward on `agg_def_rapm`); the client recomputes it
live under roster edits (`computeRating` returns `ratingRapm`). **Display only, never an input** —
re-weighting the blend toward RAPM did not clear the gate (`scripts/gate_blend_weight.py`).


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
market on aggregate accuracy (7.62 vs 6.88 MAE), that single-player what-ifs extrapolate the
calibration slope further than the backtest validated, and (upcoming season) that the market
ring is shown-only and never an input. Do not remove them.

**Deployment: Vercel, private repo, `ui/` only.** GitHub Pages free requires a public repo;
Vercel Hobby deploys the private repo and still serves a public URL. The site is configured
to serve **only `ui/`** (Vercel *Root Directory* = `ui`), so `data/`, `nbaproj/`, and
`scripts/` are never uploaded or reachable. `ui/vercel.json` serves `projections.html` at
`/`; `ui/.vercelignore` keeps `build.py`/`template.html` out. Full steps in
[ui/DEPLOY.md](ui/DEPLOY.md).

**Freemium gate (shipped 2026-08-02).** `ui/build.py` splits the bundle into a **public** payload
(inlined in `projections.html`: bars, ranges, off/def + RAPM-arm readouts, market comparison,
Track-record + Drift views, glossary) and a **premium** payload (each team's `players` + what-if
`grid` — the expanded detail panels: per-player Off/Def and ≈Wins, RAPM flags, disagreement +
conviction, trade-undo, live editor). Premium is embedded in the serverless function
`ui/api/premium.js` and returned only when the request's `x-unlock-password` matches the
`PREMIUM_PASSWORD` env var — Vercel runs `api/*.js` as functions, so the source (and its embedded
data) is never served as a static asset. The client (`teamView`/`lockedPanel`/`doUnlock` in
`template.html`) shows the public bars to anyone; "Unlock details" fetches the premium payload,
caches it in `sessionStorage`, and re-renders with full panels. Real server-enforced gating; still
one **shared** password (no accounts). Next step for real subscriptions: per-user auth
(Clerk/Supabase) + Stripe — the serverless function is the seam to add it (see the auth roadmap item).

**✅ League-wide player-impact leaderboard (shipped 2026-08-04, `ui/nba_players/`).** A standalone,
self-contained companion view — one sortable / searchable / team- and position-filterable table of
**every rostered player's projected impact league-wide**, per season, mirroring the NHL impact viewer
(`ui/nhl/`). Columns: rank, Player (`pos · TEAM` subtitle), Impact, Off, Def, MPG, Age; default sort
Impact desc, centered mini-bar on Impact, sign-colored Off/Def/Impact, light/dark toggle, glossary +
the honest caveats (Impact is a per-100 rate not a win count; defense is the weakest metric; these are
projected talent inputs). `scripts/build_nba_players_ui.py` flattens `snapshots.json`'s per-team
`players` into one league-wide list per season (auto-detects seasons; dedupes players who appear on two
rosters in a walk-forward snapshot, preferring the row with a listed position — verified value-lossless)
and inlines it into `ui/nba_players/template.html` → `ui/nba_players/players.html` (`__DATA__`
placeholder, `allow_nan=False`). Rebuild after a snapshot rebuild:

```
python scripts/build_nba_players_ui.py
```

**Public/premium decision (owner, 2026-08-04): this leaderboard is PUBLIC.** It exposes the same
per-player Off/Def/Impact numbers that stay **premium-gated inside the team detail panels**
(`ui/api/premium.js` / the `ui/build.py` public-vs-premium split) — that team-panel gating is
**unchanged**. Only this standalone page is public: it ships with data inlined and its own Vercel route
`/players → /nba_players/players` (`ui/vercel.json`), no serverless gating. `ui/.vercelignore`'s
directory-agnostic `template.html`/`build.py` patterns already keep the build inputs out of the deploy;
`scripts/` is outside the `ui` root dir so the build script is never uploaded.

**✅ Shared cross-page nav (shipped 2026-08-05).** All three league pages
([ui/template.html](ui/template.html) → `projections.html`,
[ui/nba_players/template.html](ui/nba_players/template.html) → `players.html`,
[ui/nhl/template.html](ui/nhl/template.html) → `impact.html`) carry an identical top nav —
a `wincurve` wordmark plus links **grouped by league** (NBA ▸ Projections · Players; NHL ▸
Impact), the current page marked with `aria-current="page"` (bold + accent underline). It wires
together the routes that already existed in `ui/vercel.json` (`/` → projections, `/players`,
`/nhl`). Pure static chrome (`.site-nav`), theme-aware, uses the shared tokens each page already
defines — `--accent` on the two leaderboards, `--mkt` (same blue) on the projections page — so no
new token was added. The pages already shared the `--sans`/`--mono` stacks and neutral palette;
the nav is the missing link between them. Because the three pages' HTML is generated by a verbatim
`__DATA__` replace, the nav block was inserted **identically into both each template and its built
`.html`** (the built files can't be regenerated without the gitignored data), so a future data
rebuild flows it through unchanged.

## Layout

```
nbaproj/
  cache.py        throttled, retrying, disk-cached nba_api wrapper
  ingest.py       per-dataset pulls; availability windows as constants
  teams.py        team-season target table; franchise spine
  baselines.py    walk-forward naive baselines + noise floor
  odds.py         historical preseason win totals scraper + strict franchise join
  market_live.py  LIVE Kalshi win-total ladders -> implied distribution (downstream only)
  rosters.py      draft priors + rookie projection; known-absence AND injury-return overrides
  inseason.py     rest-of-season (in-season) model core: SRS state at game N, w(N) shrinkage
scripts/
  fetch_all.py       full historical pull (idempotent, resumable)
  baseline_report.py Stage 1 report: the bar
  fetch_market.py    pull live Kalshi lines -> data/processed/market_2026_27.json
  injury_return_candidates.py  scan for injury_returns.json candidates (data-only shortlist)
  gate_inseason_model.py       walk-forward gate for the in-season model (N=25, N=50)
  gate_inseason_v2.py          gate for the v2 per-player talent update (rejected: b->0)
  project_inseason.py          produce a rest-of-season projection bundle for a season+N
  log_projection.py            append a run to data/projection_history.json (drift time-series)
```

Override files (hand-authored, tracked in git; `data/overrides/` is NOT gitignored):
`data/overrides/known_absences.json` (players expected to miss time) and
`data/overrides/injury_returns.json` (its inverse — returning-from-injury stars).

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
| **Returning-from-injury stars** | ⚠️ manual | `data/overrides/injury_returns.json` — the inverse override (see below) |

**A projection is only as current as its snapshot date.** Trades continue all season; a
July projection cannot know about a November deal. Always record the snapshot date with
any output. Historical injury *reasons* are unavailable — Pro Sports Transactions is
behind a Cloudflare bot challenge we will not bypass — so absences mix injury with
rest, suspension and coach's decision. Hence the name "availability", not "health".

### ✅ SHIPPED: injury-return override (the inverse of known absences)

A star who missed most or all of *last* season is mis-projected three ways, all fixed by
`data/overrides/injury_returns.json` + `nbaproj/rosters.py` (`load_return_overrides`,
`resolve_return_overrides`, `injured_season_mask`, `apply_return_overrides`), wired into
`scripts/project_current.py`:

1. **Minutes** fall back to the 8.0-mpg bench default, because `prior_mpg` reads only
   `LAST_HISTORY` and he supplied ~none — Haliburton was projected at **8.0 mpg**, an
   All-Star point guard as a deep reserve. This was the catastrophic bug.
2. **Availability** is dragged down by the games-missed model (Tatum **54%**).
3. **Talent** can be distorted by a partial injured-season sample (Tatum's 16-game 2025-26
   inflated his defense to +2.09 / deflated offense to +1.40 vs a healthy +1.7 / +1.8).

Each entry re-projects the player **as his last healthy season** (`basis_season`): his
post-injury partial seasons are dropped from the projection *inputs* (`injured_season_mask`
over both the box `imp` and the RAPM frame), so the normal aging + shrinkage restore his
pre-injury off/def — box **and** RAPM — with no hand-entered impact numbers; `prior_mpg`
becomes the basis season's minutes × an optional eased-in `minute_restriction`; and
`proj_availability` is **set** to `expected_availability` (a RAISE — unlike the absence
override, which floors via `min`). The input-filtering only fires for a basis within two
seasons of TARGET (else the reprojected player fails `project_next_season`'s "seen
recently" gate); minutes/availability restore for any basis.

**Live bundle only.** Like `known_absences.json`, it is manual, forward-looking, and applied
in `project_current.py` after the model runs — it **never touches the walk-forward
backtest** (which uses real historical rosters and cannot see the future), so there is no
gate to clear; it is a judgment overlay, and the numbers are user-editable. Per-player it
emits `ret_override` / `ret_reason`; the UI shows a purple **`back`** badge (hover = reason)
and a glossary entry.

Shipped list (2026-08-02) and effect. First five are confirmed Achilles/ACL returns
(~13–19 months out by opening night); the last four were added from the candidate scan on
user selection (availability is user judgment, Porziņģis discounted for durability):

| Player | Team | Before → after wins | Driver |
|---|---|---|---|
| Jayson Tatum | BOS | 59.3 → **62.3** | avail 54→97%, mpg 32.6→36.4, split cleaned |
| Tyrese Haliburton | IND | 32.9 → **34.7** | mpg **8.0→31.9** (the fallback fix) |
| Kyrie Irving | DAL | 34.0 → **35.9** | role + availability restored (eased) |
| Damian Lillard | POR | 40.4 → **42.1** | restored but eased (36 y/o post-Achilles) |
| Fred VanVleet | HOU | 48.8 → **48.0** | see below — went *down* |
| Walker Kessler | LAL | 46.2 → **48.8** | played 5 of 82; young, near-full return |
| Jalen Williams | OKC | 58.4 → **59.6** | missed ~half; young starter restored |
| Kristaps Porziņģis | GSW | 41.8 → **43.3** | restored but eased (72%, chronic durability) |
| Domantas Sabonis | SAC | 26.1 → **27.6** | missed most of last year, restored |

**The VanVleet lesson.** Our metric rated his last healthy season (2024-25) at **−1.1
impact** (below replacement), so "restore pre-injury level" restores a slightly *negative*
rating and Houston drops 0.8 — the real effect is fixing his minutes (bench fallback → ~33
mpg), not a boost. A legitimate, explainable disagreement (the user sees him as impactful;
the metric does not), surfaced rather than papered over. This is why the override restores
what the *metric* thought, not a reputation.

`scripts/injury_return_candidates.py` is the reusable, data-only scan that surfaces the
candidate pool (rostered, missed >half of last season, positive impact in a recent healthy
one) — a shortlist to review, **not** a list to import: whether each is a genuine return at
prior level (vs chronic absence, trade, rest, or age decline) and his prognosis are manual
calls. Top names it still flags as unreviewed: Anthony Davis, Giannis, Joel Embiid (all
chronic-absence rather than clean single-injury returns), Stephen Curry (age).

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

### ✅ SHIPPED (display only): All-Defense / All-NBA eye-test badges (`nbaproj/awards.py`)

`scripts/pull_awards.py` pulls each candidate player's All-NBA and All-Defensive selections
(nba_api `PlayerAwards`, one call per player, cached; candidate pool = top-75/season by minutes
∪ ≥1500 min, which captures every honoree — verified exactly 10 All-Def and 15 All-NBA per
season). `honor_lookup` surfaces the most recent selection **strictly before** the projected
season (walk-forward), emitted per-player into the bundle as `all_def` / `all_nba` ({yr, team,
n}); the UI shows a **🛡 shield** (All-Defensive) and **★ star** (All-NBA) badge with the team
number and a hover (career count + recency), next to the existing D↑/D↓ RAPM flags.

**They are shown, not scored** — both failed as projection inputs (see the negative-results
table): prior-year All-Defense is redundant with the metric at the team level and loses the win
gate at every setting; All-NBA is an offensive honor that would mis-credit defense-poor scorers.
They earn their place as *context*: a shield next to a low `Def` is the eye test flagging a
perimeter stopper the box score misses (Herbert Jones, Holiday, Anunoby, Cason Wallace all rate
≤ 0 with a shield). The nice validation that the two badges separate the axes: Gobert carries a
9× 🛡 and no ★; Karl-Anthony Towns carries a 3× ★ and no 🛡.

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
| **Offensive-creation features** (potential/secondary assists, points created, screen assists) | Offense is already saturated: out-of-sample team-offense corr flat 0.930 → 0.930 (calibrated err 0.870 → 0.879, *worse*); win gate +0.018 (noise). Box assists + usage + efficiency already encode creation, so these are collinear refinements — Brunson barely moves (+2.09 → +2.21). Passing data pulled and kept (`player_passing`), features not added. |
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
- ✅ **Historical market lines in the per-season UI — SHIPPED (2026-07-31).** Each completed
  season's view now carries the **preseason Vegas over/under** as the hollow blue ring beside our
  mean and the actual diamond — a three-way per-team read (model vs market vs reality).
  `build_snapshots._attach_historical_market` joins `market_baseline.parquet` (`market_wins_82`,
  already 82-game-normalized) by TEAM_ID, emits `mkt_wins` + a per-snapshot `market_source`
  (`bbref:vegas_ou`). Historical is **Vegas-only** — Kalshi/Polymarket have no history. The UI
  marker/readout are now source-aware (`marketLabel`); the detailed live-Kalshi caveat stays
  upcoming-season-only, the hindsight caveat explains the Vegas ring.
- ✅ **Model-vs-market-vs-actual "Track record" view — SHIPPED (2026-07-31), extended to 7 seasons
  (2026-08-02).** A second view (Projections / Track record toggle) charts, per completed season,
  our MAE vs the Vegas MAE vs a .500 baseline, plus the honest scoreboard (**7 full seasons:
  model 7.75, Vegas 6.90, model closer in 2 of 7**) and the per-team "best calls / where the
  market won." Extended from 5 to 7 by adding the two remaining FULL seasons in the backtest
  window (2017-18, 2018-19); the shortened 2019-20/2020-21 are skipped (bubble/covid, market not
  comparable). All client-side from the inlined snapshots (`renderTrackRecord`), no new data.
  Further back is blocked on the `team_rosters` 2016-17 floor (Vegas lines go to 2005-06) — a
  historical `commonteamroster` pull would unblock the full ~19 seasons.
- ✅ **Projection time series + drift charts — SHIPPED (2026-08-02).** `scripts/log_projection.py`
  appends each run of `project_current.py` (preseason) or `project_inseason.py` (in-season) to
  **`data/projection_history.json`** — a compact per-team record keyed by (run_date, model, season),
  idempotent per key. It lives at the `data/` root (**tracked**, not under gitignored
  `data/processed/`), so the series persists across checkouts/deploys, like the override files.
  `build_snapshots.py` inlines it (`projection_history`) into `snapshots.json`. A new **Drift** view
  (`renderDrift`, third toggle beside Projections / Track record) charts, per team, projected wins
  over run dates as small-multiple SVG sparklines (latest wins + change-since-first chip), sorted by
  latest wins, shared y-axis. Client-side from the inlined history. **Honest scope caveat surfaced in
  the view (`modelNote`):** a *preseason*-model run's drift is a **roster-composition** signal
  (trades/injuries/overrides), NOT this-year form (the preseason model has no in-season updating); an
  *in-season*-model run's drift additionally reflects **team form**. Seeded with the current
  preseason run (1 point); it fills in as you re-run on a cadence (monthly offseason; ~25 & ~50 games
  in-season, esp. post trade deadline).
- ✅ **Trade "undo" — SHIPPED as offseason-move undo (2026-08-02).** Each team's expanded panel has
  an **Offseason moves** section: **arrivals** (players whose last-season team ≠ current team) and
  **departures** (were here last season, now elsewhere), each a one-click chip. Clicking undoes the
  move through the *existing* what-if recompute as a **two-sided edit** — an arrival goes back to his
  old team, a departure returns here — so **both** teams reprice and show their green/red delta in
  the list. `project_current.py` emits per-player `prev`/`prevId`; the client does the rest
  (`movesSection`, the `data-undo-arr` / `data-undo-dep` handlers reuse
  `state`/`computeRating`/`winsAt`). Filtered to ≥10 mpg so camp bodies don't clutter it. **Honest
  scope, stated in the UI:** this is roster MOVEMENT (trades + free agency + waivers combined) —
  transaction *type* is not paired into two-for-one trades — but it is now correctly scoped to
  **this offseason only** (see next).
  - ✅ **Offseason-vs-mid-season correctness (2026-08-02).** The first cut computed `prev` as the
    *last-season primary team by minutes*, which mislabeled last season's **trade-deadline / buyout**
    movement as fresh offseason moves — e.g. Jared McCain showed as an OKC "offseason arrival from
    PHI" and PHI as losing him, when he was actually **"Traded from PHI on 02/04/26"** (deadline) and
    played half of last season for OKC; likewise Jeremy Sochan ("Signed on 02/13/26"). Two fixes,
    both keyed off the roster snapshot's **`HOW_ACQUIRED`** field (which carries the transaction type
    **and date**, e.g. "Traded from WAS on 07/08/26"):
    1. **Restrict to true offseason moves.** A move counts only if the `HOW_ACQUIRED` date is on/after
       `OFFSEASON_START` (May 1 of the target year, i.e. after last season ends); undated rows fall
       back to *minute overlap* (a player who logged minutes with his **current** team last season
       was already here mid-season). This removed **35 of 121** mislabeled arrivals (all dated
       Dec 2025–Apr 2026: McCain, Sochan, Harden, Garland, JJJ, Trae Young, Anthony Davis — AD via
       the date, since a 0-minute deadline stint has no overlap signal).
    2. **Correct the "came from" team.** `prev`/`prevId` now uses the team the player was on at the
       **end of last season (latest game by date)**, not primary-by-minutes, and a trade's explicit
       `HOW_ACQUIRED` "from XXX" **overrides** it (authoritative even after a 0-minute deadline stint).
       This fixed all **10** free-agent multi-team cases (e.g. Vučević ← CHI→**BOS**, D'Angelo Russell
       ← DAL→**WAS**) and *recovered* 1 genuine offseason move the old logic missed entirely (Khris
       Middleton went WAS→DAL at the deadline then **back to WAS** this offseason; primary-by-minutes
       was WAS = current, so he showed as neither arrival nor departure). Result: 87 arrivals, with
       departures conserved exactly (87 == 87). Display-only metadata; the projection is unchanged
       (sum still 1230.5 wins).
  ⬜ **Future refinements** (need a transactions feed we don't have): true two-for-one trade
  *pairing*; and in-season trades will also fall out for free by diffing periodic roster snapshots
  once the projection-history logging captures rosters over time (it currently logs per-team
  projections, not rosters).
- ✅ **In-season / rest-of-season projection model — SHIPPED (2026-08-02; the big one).** A
  genuinely NEW model (its own calibration + its own walk-forward gate, not a flag on the preseason
  one), for runs at ~25 games and ~50 games (post trade deadline). Core is a **regression-to-the-
  mean shrinkage estimator** (`nbaproj/inseason.py`):

      rest_rating[T] = w(N)·obs_rating[T] + (1−w(N))·preseason_prior[T]  →  simulate remaining games

  `obs_rating` = a schedule-adjusted SRS (simple rating system) built from only the games before a
  calendar split date `d_N` (median team's Nth-game date, so remaining-game matchups stay coherent
  for the sim), ×1.017 onto the prior's per-100 net-rating-deviation scale. `preseason_prior` is the
  shipped walk-forward projection (reused verbatim). `w(N)` RISES with games played — a hot start is
  part skill, part luck — and is fit walk-forward by minimizing rest-of-season **win** MAE directly
  (a fast deterministic expected-wins objective, NOT a rating-space proxy, which has misled this
  project three times). Banked wins add back with zero error, so the honest metric is **rest-of-
  season MAE**, never full-season MAE (which mechanically collapses as banked share grows).

  **Gate (`scripts/gate_inseason_model.py`, 5000-sim walk-forward, 6 folds exShort):** beats both
  baselines — preseason-carried-forward AND naive current-pace —

  | split | model | vs preseason-carried-fwd | vs naive pace | fitted w |
  |---|---|---|---|---|
  | N=25 | 5.17 | **+0.75 ±0.15, 6/6** | **+0.43 ±0.20, 5/6** | 0.35 |
  | N=50 | 3.27 | **+0.84 ±0.12, 6/6** | −0.06 ±0.05, 3/6 (≈tie) | 0.83 |

  It crushes the stale prior at both splits, and the regression-to-mean does real work at N=25
  (beats naive pace by +0.43, ~2 SE); by N=50 the season has spoken and w→0.83, so naive is ~optimal
  — exactly the predicted shape. `w` rising 0.35→0.83 as games double is textbook shrinkage.

  **Runnable:** `scripts/project_inseason.py --season <s> --games <N>` produces a rest-of-season
  bundle (banked / projected-remaining / projected-full wins per team, `w`, obs-vs-prior rating),
  validated on completed seasons (2024-25 @ N=25: sum=1230, OKC's 19-6 start correctly regressed to
  a 61-win projection). Live use once 2026-27 games are pulled into `game_log`; the remaining
  schedule comes from the real post-split games (exact) or a prior-season stand-in (flagged) before
  a schedule pull.

  **v1 is TEAM-RESULTS only** — the big, cheap lever. ⚠️ **v2 (per-player in-season talent update)
  — BUILT, GATED, REJECTED (2026-08-02).** Blended each player's through-N box production into his
  preseason-projected impact (k = poss/(poss+800), DARKO-style; scored with build_impact's own
  fitted coefs, on/off + tracking held at preseason since they aren't computable per-player mid-
  season), re-aggregated the current roster by through-N minutes, and added it as a third arm:
  `rest_rating = a·SRS + b·player_updated + (1−a−b)·prior`, (a,b) fit walk-forward
  (`scripts/gate_inseason_v2.py`). **The fit put b→0: N=25 b=0.00 (v2 ≡ v1, 6/6), N=50 b=0.03
  (−0.012, 0/6), and the churn subgroup showed no benefit either.** Three reasons, all instructive:
  (1) the team SRS already saturates the rest-of-season signal; (2) the player arm is defense-thin
  (compresses stars toward zero); (3) v2-as-built uses the through-N roster, which **cannot see a
  deadline acquisition** (the incoming player hasn't played for the new team yet), so the one case
  it was meant to help isn't reachable. Same shape as the RAPM-blend/defensive negatives: real at
  the player level, dies at the team-win number. **Untested redemption path** (not built; narrow
  value): a post-deadline split using the *current* (post-trade) roster snapshot instead of the
  through-N roster, restricted to actual deadline-trade teams. Machinery + gate kept for that.
- ✅ **Defensive-metric experiments — ALL FIVE RESOLVED (user-requested 2026-08-02).** Defense is
  the model's weakest link; a parallel scoping workflow pre-checked all five untried directions, and
  each was then taken to the point its verdict was decisive. **The meta-finding: after this cycle's
  box-metric fixes (position-relative rebounding + rim/hustle tracking + BPM position fallback +
  RAPM-prior refit), the defensive metric sits at a local optimum — every further direction is
  neutral-to-negative at the team-win level.** Details per item and the "why" are in the
  measured-negative-results table above (five new rows: position-relative shot defense, player-level
  pure-RAPM refit, multi-season RAPM, matchup data).
  1. ✅ **SHIPPED: refit the box-informed RAPM prior on the CURRENT box defense.** +0.036 wins
     (7.61 → 7.58); `scripts/build_rapm.py` is the canonical generator. Only positive result.
  2. ⚠️ **SKIPPED at pre-check: multi-season (2-3yr, decayed) RAPM.** Worse than single-season in
     all 6 variant-transitions; box now out-predicts every RAPM variant (`scripts/precheck_multiseason_rapm.py`).
  3. ⚠️ **REJECTED (feature); data KEPT: nba_api matchup data.** `MatchupsRollup` wired into ingest
     (`nbaproj.ingest.matchups`, `data/processed/matchups.parquet`, 2017-18+), but the containment
     feature `m_fgpct` fails YoY stability even pooled (r²≈0.05-0.08 — DRAYMOND redux); the stable
     feature restates steals (`scripts/precheck_matchup_defense.py`).
  4. ⚠️ **GATED, REJECTED: player-level box→pure-RAPM refit with team FE.** Mechanism works (dreb
     weight collapses) but the win is circular and the honest box-only version is neutral-to-worse
     at the win level (7.59 → 7.65/7.69, `scripts/gate_defense_playerlevel_refit.py`).
  5. ⚠️ **GATED, REJECTED: position-relative shot-defense features.** Position-relative *does*
     neutralize the confound damage (the league-wide version was −0.054; this is −0.025, within
     noise) but yields no net gain — redundant with the shipped rim tracking
     (`scripts/gate_shot_defense_posrel.py`).
- ✅ **Auth / freemium access — SHIPPED as serverless + shared password (2026-08-02).** Chosen from
  four options (soft reveal / static-encrypted / **serverless+password** / managed-auth+Stripe); the
  user picked serverless+password for real gating with the cleanest runway to per-user auth. The
  bundle is split (`ui/build.py`) into a **public** inlined payload (bars, ranges, readouts, Track
  record, Drift, glossary) and a **premium** payload (each team's `players` + what-if `grid` → the
  expanded detail panels) embedded in the Vercel serverless function `ui/api/premium.js`, returned
  only when `x-unlock-password` matches the `PREMIUM_PASSWORD` env var. Vercel runs `api/*.js` as
  functions, so the source + embedded data are never served statically — real server-enforced gating
  (unlike inlining, which view-source exposes). Client: `teamView`/`lockedPanel`/`doUnlock` in
  `template.html` — public bars for anyone; "Unlock details" fetches + caches (`sessionStorage`) +
  re-renders full panels. Verified end-to-end (locked path: bars render, panels locked; unlock:
  premium merges, 19-row rosters + moves + disagreement + live recompute). Set-up in
  [ui/DEPLOY.md](ui/DEPLOY.md).
  ⬜ **Next for real subscriptions** (the shared password is one-for-all, no accounts): swap the
  password check in `ui/api/premium.js` for **per-user auth** (Clerk / Supabase / Auth0 — verify a
  session token instead of a shared password) and add **Stripe** billing to gate on subscription
  status. The serverless function is the seam. **Payment/Stripe must be wired by the user** —
  handling payment credentials is out of scope for the assistant.

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
