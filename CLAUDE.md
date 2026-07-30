# NBA Season Record Projection System

## What this project is

Projects each NBA team's regular-season record for an upcoming season as a
**probability distribution** (not a single number), built bottom-up from the current
roster's players. Compares those projections against betting and prediction-market
prices (Kalshi, Polymarket, historical sportsbook win totals) to find places where an
analytically-driven model disagrees with the market.

**This is a research tool for identifying market disagreements. The user is not
betting.** The deliverable is *explainable per-team disagreement* — "we differ from
the market on this team, and here is the structural reason why."

Current projection target: **2026-27 season**.

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
A. Player talent projection   (impact per 100 possessions, aging, shrinkage)
B. Minutes & availability     (240 min/game budget, injuries, replacement level)
C. Team aggregation           (minute-weighted sum -> off/def rating)
D. Monte Carlo season sim     (real schedule, home court, rest -> win distribution)
E. Market comparison          (downstream only)
```

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

| Baseline | MAE | RMSE |
|---|---|---|
| Market (preseason win total), excl. 2019-20 | **6.67** | 8.53 |
| Market, all seasons | 6.70 | 8.57 |
| Mean-reverted previous wins ← *the naive bar* | 8.07 | 10.01 |
| Previous wins (persistence) | 8.75 | 11.19 |
| Always .500 | 10.18 | 12.30 |
| *Theoretical binomial noise floor* | *3.44* | *4.31* |

Fitted mean-reversion coefficient **k ≈ 0.62**, stable across folds (0.610–0.649):
teams retain about 62% of their distance from .500 year over year.

Implied true-talent spread is **SD ≈ 11.6 wins** vs **4.3 wins** of luck, so talent
variation dwarfs randomness and projection is worth doing.

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

## Layout

```
nbaproj/
  cache.py       throttled, retrying, disk-cached nba_api wrapper
  ingest.py      per-dataset pulls; availability windows as constants
  teams.py       team-season target table; franchise spine
  baselines.py   walk-forward naive baselines + noise floor
  odds.py        preseason win totals scraper + strict franchise join
scripts/
  fetch_all.py       full historical pull (idempotent, resumable)
  baseline_report.py Stage 1 report: the bar
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
- 🟡 **Stage 5** — Monte Carlo simulation built; **marginally still fails its gate** in
  realistic mode after the alpha fix. Identical seasons (2017-18..2025-26):

  | | MAE (wins) |
  |---|---|
  | Market | **6.88** |
  | Our model, leaky upper bound (real roster + minutes) | **7.25** |
  | Mean-reverted previous wins (the gate) | **8.13** |
  | Our model, realistic roster | **8.24** |

  The leaky bound now beats the gate clearly and comes within 0.38 of the market,
  even beating it in 3 of 9 seasons. The realistic variant is essentially tied with
  the gate. **The remaining ~1.0 win gap between the two variants is roster and
  minutes projection, not talent.**

  **Important nuance for live use:** "realistic" mode is harsher than real operation in
  one respect — it reconstructs rosters from the first 15 games, so it misses a star who
  was injured on opening night even though such a player is perfectly well known
  preseason. For a live 2026-27 projection the roster is *known*; only midseason trades
  are not. So true operational accuracy sits between 7.25 and 8.24, likely nearer 7.25.
  Using `commonteamroster` for historical seasons (~270 calls) would tighten the
  backtest to match how the model is actually used.

  Interval calibration (are the 80% ranges really 80%?) still unchecked.
- ⬜ **Stage 6** — Fit as residual structure (diminishing returns). *Gate may reject.*
- ⬜ **Stage 7** — Coaching / roster continuity as shrunk effects. *Gate may reject.*
- ⬜ **Stage 8** — Live Kalshi/Polymarket comparison + contract-year hypothesis test

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
- ⬜ 2026-27 preseason win totals not yet published on bbref (404 as of July 2026) —
  live market comparison will need Kalshi/Polymarket
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
