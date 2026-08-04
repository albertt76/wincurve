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
the direct analog of the NBA play-by-play → RAPM pipeline. Inputs are already verified
available: **shift charts** give the on-ice 5-man units (`playerId`/`startTime`/`endTime`
per shift) and **play-by-play** carries a `situationCode` (strength state), both from
2007-08 on; MoneyPuck supplies the shot xG.

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

Availability windows (empirically verified, do not assume): MoneyPuck xG, shift charts, and
play-by-play with coordinates all start **2007-08** (the real-time scoring era), so the model
**backbone is 2007-08 → 2025-26**. Team-summary records are pulled back to 2005-06 only to
give the walk-forward earlier training years. Per-game play-by-play + shift charts (the RAPM
bulk pull, thousands of games) are deliberately deferred to the impact stage; their structure
is verified (`nhl.ingest.game_pbp` / `nhl.ingest.shifts` / `nhl.ingest.roster`).

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

---

## Roadmap

- ✅ **Stage 0** — data layer: cached, throttled, point-in-time pulls; verified inventory.
- ✅ **Stage 1** — baselines: **the bar = 10.54 MAE points** (mean-reverted previous points),
  persistence 12.09, flat 12.45; k ≈ 0.52.
- ⬜ **Stage 2** — skater xG-RAPM from the play-by-play + shift-chart bulk pull; aging,
  shrinkage; off/def decouple. Goalie GSAx module.
- ⬜ **Stage 3** — TOI/role & availability; replacement level; rookie/first-year priors.
- ⬜ **Stage 4** — team aggregation (skaters + goalie + special teams → GF/GA rates),
  calibrated per fold on projected aggregates.
- ⬜ **Stage 5** — season simulation with regulation/OT/shootout → points distribution;
  interval calibration. One-year carryover.
- ⬜ **Stage 6** — market comparison (season points over/under; Cup/division/playoff odds,
  downstream only) + per-team disagreement UI.

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
