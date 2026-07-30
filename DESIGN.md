# NBA Season Record Projection System — Design

## Goal

Project each NBA team's regular-season record for an upcoming season as a **distribution**,
not a point estimate, and compare against betting/prediction-market prices (Kalshi, Polymarket,
historical Vegas win totals) to identify where an analytically-driven model disagrees with the
market. Research tool, not a betting system.

---

## 1. The binding constraint: degrees of freedom

This is the single most important design fact, and it dictates the architecture.

| Level | Rows available (21 seasons, 2005-06 → 2025-26) |
|---|---|
| Team-seasons | **630** (21 × 30) |
| Player-seasons | **10,595** (458–569/season) |
| Player-season transitions (year N → N+1) | ~8,500 |
| Team-seasons with tracking data | **390** (13 × 30) |
| Team-seasons with hustle data | **300** (10 × 30) |

Every team-level feature in the original spec — positional balance, defensive/offensive
balance, rebounding balance, pace, turnover type, coaching quality, fit — competes for the
same **630 rows**. And the target is noisy: an 82-game record at p≈0.5 carries roughly
±4.3 wins of pure binomial noise before injuries, which add more (both figures measured in
Stage 1, not assumed).

**Consequence:** a team-level model with 30+ features cannot be fit here. It will overfit and
backtest beautifully in-sample while being worthless out-of-sample.

**Therefore:** all heavy lifting happens at the **player level** (~10,600 rows), and the
team layer must be nearly parameter-free — mechanical aggregation plus simulation. Fit and
coaching enter as a *small* number of tightly-constrained parameters that must earn their
place in a backtest.

---

## 2. Architecture

```
                    ┌──────────────────────────────────────┐
                    │  A. Player talent projection         │
                    │     impact per 100 possessions       │
                    │     (aging, regression, uncertainty) │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  B. Minutes & availability            │
                    │     240 min/game budget constraint    │
                    │     injury model, replacement level   │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  C. Team strength aggregation         │
                    │     minute-weighted sum → ORtg/DRtg   │
                    │     + fit adjustment (small)          │
                    │     + coaching adjustment (smaller)   │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  D. Monte Carlo season simulation     │
                    │     real schedule, HCA, rest, travel  │
                    │     → win distribution per team       │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  E. Market comparison (strictly       │
                    │     downstream — never a feature)     │
                    └──────────────────────────────────────┘
```

The critical property: **C is almost parameter-free.** Minute-weighted sums of player impact
predict team net rating remarkably well. That linearity is what makes the 630-row problem
tractable.

### D is a simulation, not a regression

Do not regress wins on team rating. Simulate all 1,230 games with per-game noise. Reasons:
1. Win totals are priced as distributions; we need P(wins > 45), not just E[wins].
2. Schedule strength differs materially between teams.
3. Non-linearity at the tails (a 65-win team's marginal rating point is worth fewer wins).
4. Injuries are a *distribution* over rosters — resample them per simulated season.

---

## 3. Honest assessment of the requested factors

Ranked by expected contribution to out-of-sample accuracy.

### HIGH — dominates the error budget

- **Minutes & availability projection.** Probably co-equal with talent as an error source and
  routinely under-modeled. Who plays 2,400 minutes vs 900 matters as much as how good they are.
- **Aging curves.** Well-supported, large effects. See §4 for the survivorship trap.
- **Regression to the mean / uncertainty.** A single season of impact metric is noisy;
  shrinkage toward a position/age/pedigree prior is essential.
- **Schedule + home court in simulation.**

### MEDIUM — real but small, model with heavy shrinkage

- **Skill scarcity (the "no competent PG" problem).** Real, but the mechanism is *shot creation
  scarcity*, not the position label. Positions are nearly meaningless now; model archetypes from
  tracking data instead. Expect ~1–3 wins for extreme cases.
- **Coaching.** Genuinely hard: we have ~10–30 team-seasons per coach, entangled with roster
  quality. Best treated as a shrunk random effect. Note that **roster continuity / turnover** is
  better documented than coach identity and is much easier to measure — model it first.
- **Health history.** The age-interaction hypothesis (older players more damaged by injury
  history) is plausible and testable. Note the main signal is usually *recent games missed*,
  and soft-tissue/back/knee injuries carry more forward signal than acute/impact injuries.

### LOW — likely noise; test, don't assume

- **Contract-year and post-extension effects.** This is a strongly-held popular belief that has
  a poor replication record; measured effects tend to shrink toward zero once age and role
  change are controlled for. It is a good *hypothesis to test explicitly* — and a genuinely
  interesting result either way — but it should not be assumed into the model. Note the obvious
  confound: players get paid *after* a career-best season, so apparent post-contract decline is
  substantially regression to the mean.
- **Fine-grained fit interaction matrices.** Highest overfitting risk in the whole project.

### ⚠️ The double-counting trap

The original spec mixes two levels of abstraction. A plus-minus-style impact metric
**already contains** the player's rebounding, turnovers, and shooting contributions. Adding
separate team rebounding and turnover features on top of aggregated player impact
double-counts them and will actively degrade the model.

**Reframing — this is the key idea:**

> **Fit is not a set of features to add. Fit is the residual that additive talent
> aggregation fails to explain.**

So: build the additive baseline first, then ask whether its residuals have structure that
balance/fit features can explain. This makes fit *empirically decidable* rather than an
article of faith, and it prevents double-counting by construction.

### What fit actually looks like when formulated this way

The best-documented non-additivities in basketball are **diminishing and increasing returns**
on a few skill dimensions:

| Dimension | Expected shape |
|---|---|
| Defensive rebounding | Strong **diminishing** returns — two elite rebounders don't stack |
| Shot creation / usage | **Diminishing** — total < sum of parts |
| Rim protection | **Diminishing** — one anchor captures most of the value |
| Floor spacing / 3P shooting | **Increasing** (complementary) — spacing amplifies rim pressure |
| Perimeter on-ball defense | Mildly diminishing |

That's ~5–8 parameters (one curvature term per dimension), which 630 team-seasons *can*
support. A full interaction matrix cannot. This is the tractable version of the fit idea.

---

## 4. Known statistical traps

1. **Survivorship bias in aging curves.** Naive year-over-year deltas are biased because weak
   players get cut — surviving 34-year-olds are selected for being good, making decline look
   milder than it is. Use the delta method with proper weighting, or better, fit aging as a
   smooth spline inside a hierarchical model with player random intercepts.
2. **Peak age is earlier than folk wisdom.** Overall impact tends to peak around 26–27, not
   28–30. And peaks differ by skill: 3P shooting holds up late (~29–31), while rim finishing
   and defensive mobility decline earlier. Model aging **per skill**, not as one curve.
   Rookie→Y2 and Y2→Y3 jumps are large — and defense does appear more experience-driven than
   offense, consistent with the original intuition.
3. **Era drift.** The league changed enormously over 20 years (pace ~90 → ~100 possessions,
   3PA ~16 → ~38 per game). Normalize per-100 and z-score *within season*.
4. **Minutes must sum to 240 per game.** Not a detail — it's the mechanism that makes team
   aggregation work. Projected minutes must be renormalized to the team budget, which
   automatically creates the "someone absorbs the injured player's minutes" behavior.
5. **Replacement level.** Needed so injury-adjusted projections are coherent.
6. **Coach's revealed preference leaks.** Projecting minutes from last year's minutes imports
   information the talent model doesn't have. Useful, but don't then treat minutes as
   independent evidence of talent.
7. **Market leakage.** Build market-blind. If market prices touch training, the comparison in
   §E is circular and meaningless.
8. **Point-in-time correctness.** For backtesting, features must use *only* information
   available before that season started. This is the easiest way to accidentally fake a good
   backtest.

---

## 5. Data inventory (availability verified)

### Verified working
- **`nba_api`** (stats.nba.com) — advanced player stats confirmed back to **2005-06**
  (458 players) through **2024-25** (569), 79 columns incl. usage, OREB%/DREB%, on-court
  ORtg/DRtg/NetRtg. Also: play-by-play, lineups, on/off, schedules, box scores.
  Rate-limited — needs throttling, retries, and a local cache.
- **Shot & defensive tracking** — `2013-14` onward (12 seasons). Rim deterrence
  (defense <6ft), catch-and-shoot vs pull-up, touch time, dribbles.
- **Hustle stats** — `2016-17` onward for full coverage (2015-16 is a partial rollout,
  147 rows). Screen assists, deflections, contested shots, loose balls.
- **Kalshi API** — `api.elections.kalshi.com/trade-api/v2` returns HTTP 200 unauthenticated
  for `/series` and `/markets`. Read-only market discovery works without credentials.
- **Polymarket Gamma API** — `gamma-api.polymarket.com/markets` returns data unauthenticated.

### Needs care
- **Basketball-Reference** — `robots.txt` **disallows** `*/on-off/`, `*/lineups/`,
  `*/gamelog/`, `*/splits/`, `*/shooting/` and `/basketball/`; `Crawl-delay: 3`.
  Season pages (`/leagues/NBA_2025_advanced.html`) and coach pages are allowed.
  → Restrict to allowed paths, honor crawl-delay, cache aggressively. Get gamelogs,
  on/off, and lineups from `nba_api` instead.
- **Pro Sports Transactions** (historical injury/inactive lists) — returned **HTTP 403** to a
  bare request; likely needs a normal User-Agent. Best available historical injury source.
  Fallback: derive games-missed from box-score absences via `nba_api`, which is ToS-clean but
  loses the injury *reason* (and reason is what carries forward signal).

### Still to source
- **Public impact metrics** — DARKO, EPM, LEBRON, RAPM archives. Licensing varies; EPM is
  partly paywalled. BPM/VORP are computable from bbref-allowed pages.
- **Contract/salary history** — needed only to test the contract-year hypothesis. Spotrac /
  HoopsHype. Historically messy; treat as optional.
- **Historical closing win totals** — for the market baseline. Not in Kalshi/Polymarket
  history (both are recent); needs a Vegas archive.

---

## 6. Build plan — each stage gated on a backtest

Backtest protocol throughout: **walk-forward**, train on seasons ≤ N, predict N+1, for
N = 2013…2024. Report MAE and RMSE in wins, plus calibration (are our 80% intervals
actually 80%?). Compare against three baselines.

Current outcomes below. See CLAUDE.md for the live numbers; this table records how each
stage resolved against its gate.

| Stage | Deliverable | Outcome |
|---|---|---|
| **0** | Data layer, point-in-time-correct, 2005-06→2025-26 | ✅ 21 seasons, 10.6k player-seasons cached |
| **1** | Baselines: prev wins, mean-reverted, market | ✅ Gate = mean-reverted (8.13 MAE); market = 6.88 |
| **2** | Player impact + aging + shrinkage | ✅ Beats "reuse last season" by 9.4% year-over-year |
| **3** | Minutes / availability incl. injury history | ✅ Availability MAE 0.186; replacement level −1.45 |
| **4** | Additive team aggregation → net rating | ✅ Near-parameter-free; calibration fit per fold |
| **5** | Monte Carlo sim → calibrated win distributions | ✅ Shipped; 80% intervals at 79.6% coverage |
| **5b** | One-year residual carryover | ✅ **8.30 → 7.95 MAE**; the one verified win |
| **6** | Fit as residual structure (diminishing returns) | ❌ **Rejected** — null out-of-sample, sign opposite hypothesis |
| **7** | Coaching / continuity as shrunk effects | 🟡 Trait is real but hurt the backtest; blocked on mis-dated coach data |
| **8** | Market comparison + contract-year test | 🟡 Historical + live projections done; live Kalshi/Polymarket pending |
| **RAPM** | Play-by-play defensive plus-minus | 🟡 Pipeline + box-informed estimator validated; integration gated on overnight pull |

Stage 6 was **rejected by its own gate** — a successful outcome, not a failure: it's the
finding. The residual it was meant to explain turned out to be one-year memory (Stage 5b),
not a diminishing-returns "fit" structure.

---

## 7. Realistic expectations

**Measured (Stage 1):** the market's preseason win totals achieve 6.67 MAE (mean
absolute error -- average miss in wins) against 8.07 for a mean-reverted naive
baseline, over 2013-14..2025-26 excluding 2019-20.

The binomial noise floor computed in §1 (3.44 MAE) is **not an achievable target**.
It assumes we know each team's true strength in October *and* that it holds constant
all season. Rosters change through unforeseeable injuries and trades, so the real
frontier is well above it, and 6.67 is likely close. Do not read "market captures
only 30% of the gap to the floor" as an opportunity estimate.

Season win totals are among the sharpest markets in sports — they're posted months ahead,
heavily bet, and slow-moving. Published attempts to systematically beat them mostly fail,
and the market sits close to the irreducible noise floor described in §1.

So the honest framing, which matches the stated goal:

- **Unlikely:** systematically beating the market on aggregate accuracy.
- **Achievable and interesting:** finding *specific, explainable* disagreements — teams where
  our bottom-up roster construction says something structurally different from the price, and
  being able to attribute *why* (e.g. "market is pricing continuity we think is talent," or
  "market hasn't discounted this aging curve").

The per-team disagreement attribution is the actual product. Aggregate MAE is how we earn the
right to trust it.

---

## 7b. Regime change: the "3-2-1" draft lottery reform (verified, no historical analogue)

Source: [nba.com, "Inside the NBA's plan to revamp the Draft Lottery"](https://www.nba.com/news/what-nba-had-in-mind-with-draft-lottery-reform),
updated 6 June 2026. Approved by the Board of Governors on **28 May 2026**, effective for
the **2027, 2028 and 2029 drafts** — a three-year trial.

**This makes 2026-27 the first season played under the new incentives**, since the
2026-27 standings determine the 2027 draft. It is exactly our projection target.

### What actually changed

Each of the 16 lottery teams gets three, two, or one lottery balls:

| Group | Balls | Odds of #1 pick | Top-5 | Top-10 |
|---|---|---|---|---|
| **Three worst records** | **2** | **5.4%** | 28% | 61% |
| Next seven non-play-in teams | **3** | **8.1%** | 39% | 73% |
| Play-in seeds 9-10 | 2 | 5.4% | 28% | 59% |
| Losers of 7-8 play-in games | 1 | 2.7% | 15% | 35% |

**The critical feature is that the incentive is inverted, not merely flattened.** The three
worst teams now have *worse* odds (5.4%) than teams with the 4th-10th worst records (8.1%).
Being the very worst team is now actively harmful. Additional provisions:

- The bottom three are **"draft relegated"** — their pick can fall as far as #12
  (everyone else is capped at #16).
- No pick may be **#1 in two consecutive drafts**, or **top-5 in three consecutive**.
- Newly traded picks cannot carry top-12 through top-15 protections.
- The commissioner gains authority to **reduce lottery odds, modify draft positions, and
  fine** teams for tanking behaviour.

Context on how real the prior behaviour was: in February 2026 the league fined **Utah
$500,000 and Indiana $100,000** for benching players who could have helped them win.

### What this implies for the projection — and it is not a uniform shrinkage

The game theory is genuinely non-monotonic, and this is the part a naive "compress the
bottom" adjustment would get wrong:

- **Teams projected in the bottom 3** now have a strong incentive to *win more*, to escape
  relegation and reach the 3-ball group. Adjust their projections **upward**.
- **Teams projected 4th-10th worst** sit in the group with the league's *best* odds and
  have no reason to tank further — indeed tanking risks falling into the penalised bottom
  three. Expect **little or no change**, possibly a slight incentive to stay put.
- **Everyone else** is unaffected.

So the expected shape is a **rising floor**: materially fewer sub-20-win teams, with the
middle and top of the league largely untouched. League-average wins is fixed at 41 by
construction, so any upward adjustment at the bottom must be offset somewhere — most
plausibly spread thinly across the teams that beat up on tanking opponents.

### Discipline for something that cannot be backtested

There is no historical season under these rules, so this is the one component that cannot
clear a walk-forward gate. Therefore:

1. It must be an **explicit, separately-reported adjustment**, never silently folded in.
   Projections should be available with and without it.
2. Its **size should be anchored on the measured historical tanking effect** — how much
   late-season underperformance out-of-contention teams currently show, by
   difference-in-differences against teams still competing. That is an upper bound on what
   removing the incentive can recover.
3. **The 2019 reform is the natural experiment.** It flattened top-3 odds to 14/14/14%.
   If tanking measurably fell afterwards, that calibrates how much this larger change
   might do; if it did not, that is evidence this reform will also underdeliver. Note this
   reform is considerably stronger than 2019's — inverting an incentive is not the same as
   flattening it — so 2019 likely gives a *lower* bound.
4. Watch the **observable signature**: minutes given to rookies and very low-experience
   players by out-of-contention teams. If that share falls in 2026-27, the reform is
   working; it is both a mechanism and a mid-season validation check.

**Standing caution:** this is the only place in the model extrapolating beyond the data.
Any projection leaning on it must say so.

## 7c. Depth, talent concentration, and star reliability

Our aggregation is linear: team rating is the minute-weighted mean of player impacts.
That treats a roster with one +8 star and four replacement players as equivalent to one
with five +1.6 players. Real basketball may not.

Three related but distinct questions, which must not be conflated:

**(a) Does concentration shift the expected number of wins?** A mean effect. Testable as
structure in our residuals.

**(b) Does concentration widen the range of outcomes?** A *variance* effect, and arguably
the more important one for market comparison, since win totals are priced as
distributions. A team whose value sits in one player is genuinely less predictable: his
health swings the season. A deep team should be tighter. If real, this means
`sigma_rating` in the simulation should vary by roster shape rather than being one number
for all 30 teams.

**(c) Is the *realized* penalty for star absence as steep as linear aggregation implies?**
When a star misses games, his minutes redistribute to teammates rather than vanishing.
If those teammates are good, the drop is shallower than the linear model predicts.

The motivating observations, which point in different directions and are worth keeping
distinct:

- **Oklahoma City 2024-25** were exceptionally deep, which let them rest players and
  absorb their second-best player's absences. They then traded depth away while getting
  that player back for a full season -- so depth and star-availability moved in opposite
  directions, and a model with only a talent-sum would see just the net.
- **Boston without Jayson Tatum** substantially outperformed expectations. This is the
  clearest evidence for (c): the linear penalty for losing a star may be too harsh.
- **A hypothetical extended Luka Doncic absence** would obviously hurt the Lakers -- but
  the question is whether it hurts by the linear amount, more, or less.

Note the tension: (b) says concentrated teams are *riskier*, while (c) says the downside
when the risk materialises is *smaller* than we would model. Both can be true, and
conflating them would cancel out a real effect. They are measured separately.

**Discipline:** (a) and (c) are backtestable and must clear the standard walk-forward
gate. Beware that concentration correlates with team quality -- good teams have more good
players -- so any claimed effect must be shown to survive controlling for projected team
strength. With only ~270 team-seasons, an in-sample correlation here means almost nothing.

## 8. Open questions

See the accompanying conversation — key forks are (1) build vs. ingest the player impact
metric, (2) how aggressive to be on data acquisition given ToS constraints, and (3) whether
current-season in-play updating is in scope or strictly preseason projection.
