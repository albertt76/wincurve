# NHL season-points projection — DESIGN

Project each NHL team's regular-season **standings points** for an upcoming season as a
probability distribution, built bottom-up from the current roster's players, and compare
against betting/prediction markets to find explainable per-team disagreements. Same
research goal and method as the NBA project (`../DESIGN.md`, `../AI_ENGINEERING.md`) — **not a
betting tool** — adapted to hockey.

All statistical acronyms are expanded on first use, per the user's standing preference
(see root `AI_ENGINEERING.md`). This applies to script output too — reports carry their own legend.

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

## Status (2026-09-28)

Stage write-ups live in `docs/nhl/` (read on demand; index at the bottom).

| Stage | Status | Result |
|---|---|---|
| 0 — data layer | ✅ | MoneyPuck xG 2007-08+; shift charts only 2010-11+ (the RAPM floor) |
| 1 — the bar | ✅ | **10.54 MAE points** (mean-reverted previous points); reversion k ≈ 0.52 |
| 2 — impact metric | ✅ | skater xG-RAPM + goalie GSAx, face-validated on 2023-24; Players viewer at `/nhl` |
| 3 — impact refinement | ✅ | pooled 3-season RAPM (+0.074 next-season corr, 6/6 folds) + box offensive prior (+0.016) → `rapm.talent()`; aging curves; forward projection (offense β ≈ 0.62, defense β ≈ 0.31, calibration slope 1.02) |
| 3b — honest roster | ✅ | opening-day roster from shift-chart debuts, weighted by prior-season TOI: 10.61 over 9 folds (the roster leak was worth ~nothing) |
| 4 — aggregation + carryover | ✅ | minute-weighted mean with replacement fill; one-year carryover rho ≈ 0.37 |
| 5 — season simulation | ✅ shipped | **10.50 MAE points** over 9 full-season folds; nominal 80% interval covers 0.82 |
| 6 — live projection + market + Records page | 🟡 | live 2026-27 projection, `/nhl/records` with roster detail and a what-if editor; live Kalshi points ladder (Vegas opener as fallback); historical Vegas **10.47 vs our 10.50 — a statistical tie** |

## The shipped model

1. **Skater talent** — `rapm.talent(end_year)`: 5v5 xG-RAPM pooled over a trailing 3 seasons
   (recency decay 0.75), with a skater's individual xG blended into offense (weight 0.4).
2. **Projection** — `projection.project(end_year)` = per-skill `beta · (talent + aging)`; defense
   is regressed hard because it is half as persistent.
3. **Roster and minutes** — backtest: `rosters.opening_roster` (season debut for the team, within
   its first 20 games) weighted by prior-season 5v5 TOI; live: `rosters.live_roster` /
   `live_toi` from the NHL web API. **Once the season starts that API roster is the ~23-man active
   list**, so injured / non-roster skaters are added back from the hand-curated
   `data/overrides/nhl_injured_nonroster.json` (the league's opening-roster release), counted at
   last season's minutes only if they have some — mirroring the backtest's first-20-games rule.
4. **Team strength** — `aggregate.team_ratings`, a minute-weighted mean with replacement level for
   uncovered minutes.
5. **Goals and points** — offense → goals-for and defense → goals-against with separate slopes,
   drift-tracked to the prior season's league scoring; `nhl/gamesim.py` simulates Poisson games
   with an empirical OT/SO share (~0.23) and 2/1/0 points; plus the one-year carryover and a
   walk-forward interval.

`nhl/season.py` is the one reusable pipeline and reproduces the gate's 10.50 exactly, so the
backtest and the live projection cannot drift apart.

**Tried and rejected** (real signal, but redundant with the carryover — do not re-attempt blind):
goalie GSAx in goals-against (+0.06 MAE, worse; GSAx barely persists, corr ~0.13), special teams
PP/PK (+0.05, worse; persistence 0.36 ≈ carryover rho 0.37), and a box *net* prior from on-ice xG
differential (hurts thin samples). Aging is one-year-neutral (+0.002 corr) but kept as
structurally correct. Unlike the NBA, **multi-season pooled RAPM helps** here — hockey's
single-season RAPM is noisier.

## Live pipeline

```
python scripts/nhl_fetch_all.py                     # Stage 0 datasets (cached, resumable)
python scripts/nhl_fetch_shifts.py --season <yr>    # per-game shift charts (heavy, resumable)
python scripts/nhl_build_impacts.py                 # single-season xG-RAPM caches
python scripts/nhl_project_current.py [--refresh]   # -> data/nhl/processed/projection_current.json
python scripts/nhl_build_records_ui.py --market [--refresh]  # -> ui/nhl_records/records.html (/nhl/records)
python scripts/nhl_build_impact_ui.py               # -> ui/nhl/impact.html (/nhl)
```

Markets (downstream only): `nhl/market_live.py` reads Kalshi `KXNHLSEASONPTS`, a season-**points**
threshold ladder for all 32 teams (live on opening night 2026-09-29); the ring is the ladder's
**median**, because Kalshi uses one fixed 70–115 grid for every team, which truncates the tails and
squashes the mean, and rungs with a bid/ask spread above 0.30 are dropped as unpriced. A team whose
ladder is too thin to read a median falls back to `nhl/market_vegas.py`, a hand-curated,
season-keyed `LIVE_SOURCES` URL (BetOnline's 2026-07-20 opener; re-find each season). Kalshi's
`KXNHLWINS` wins series exists but has never listed an event. Historical
lines: `nhl/odds.py` (hockey-reference `/leagues/NHL_<year>_preseason_odds.html`, 2010-11+),
scored by `scripts/nhl_market_history_report.py`.

## Open items (⬜)

- A "Track record" UI view for the historical Vegas comparison — the report script is the data,
  not yet a page.
- Injury overlays beyond the roster snapshot: `nhl_injured_nonroster.json` only keeps injured
  skaters on the roster at full weight; there is no partial-season availability (a long injury to a
  player who played last season still counts him fully) and no known-absence file. Re-curate the
  injured list on every roster re-pull — the API roster changes daily in-season.
- Not attempted: real strength of schedule (a balanced schedule is assumed), a trade editor as the
  what-if editor's v2, pre-2010 shift data from the NHL HTML shift reports, and a proper trinomial
  noise floor.

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
  point), so the binomial noise floor is only an approximate upper bound. The points-distribution
  simulator shipped in Stage 5; a proper trinomial floor is still later work.
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

## Detail docs (read on demand)

| File | Covers |
|---|---|
| `docs/nhl/data-and-impact.md` | Stage 0 inventory and shift gaps, the Stage 1 bar, Stage 2 estimators and the Players viewer, Stage 3 refinement steps, Stage 3b honest roster |
| `docs/nhl/team-model.md` | Stage 4 aggregation, replacement level, goalie persistence, carryover; Stage 5 game model and the rejected levers |
| `docs/nhl/live-and-market.md` | Stage 6 live projection, Records page, wins-vs-points reconciliation, roster detail, what-if editor, Vegas integration |
