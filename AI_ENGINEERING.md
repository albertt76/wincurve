# AI Engineering Guide

## Purpose and authority

This is the provider-neutral engineering guide for this repository. It applies to humans and all
AI assistants. Read it before planning or changing anything.

- Keep assistant-specific workflow and delegation rules in `CLAUDE.md`.
- Treat the code (`nbaproj/`, `nhl/`, `scripts/`, `ui/*/template.html`) as authoritative when a
  document disagrees with it.
- **Read the smallest relevant document before touching a subsystem** (index at the bottom) — do
  not load unrelated documentation merely for context. The `docs/nba/` files are long histories;
  search them (`grep -n`) and read the matching section rather than the whole file.
- When the model, pipeline, UI conventions, or roadmap change, update this guide and the matching
  `docs/nba/` file in the same commit. Mark roadmap items ✅ / ⬜ in `docs/nba/roadmap.md`.

## What this project is

**A multi-sport season-record projection system.** NBA is the mature reference implementation
(`nbaproj/`). **NHL** is the first expansion (`nhl/`); **`nhl/DESIGN.md` is the authoritative NHL
doc — read its Status and Open items sections before NHL work; stage write-ups are in `docs/nhl/`.** NHL status:
Stages 0–5 done, Stage 6 (live projection + Records page at `/nhl/records`) underway; the model
ties the Vegas points line (10.50 vs 10.47 MAE points). Sport-agnostic code moves into `core/`
only once a second sport proves the seam (so far just `core/httpcache.py`); the NBA code is not
refactored onto `core/` until the NHL build shows what is genuinely shared. NFL and the top-5
European soccer leagues are planned. The communication and walk-forward conventions below apply to
every sport.

**NBA:** projects each team's regular-season record for an upcoming season as a **probability
distribution**, built bottom-up from the current roster's players, and compares it against
betting and prediction-market prices (Kalshi, Polymarket, historical sportsbook win totals).

**This is a research tool for identifying market disagreements. The user is not betting.** The
deliverable is *explainable per-team disagreement* — "we differ from the market on this team, and
here is the structural reason why."

Current target: **2026-27**. Shipped walk-forward backtest (2017-18..2025-26): **7.39 wins MAE**
vs the market's **6.88**; the gate every stage must clear (mean-reverted previous wins) is
**8.13**. We do not beat the market on aggregate accuracy and should not expect to — realistic
improvement is a few tenths to ~1 win and may be zero, and the 3.44 binomial noise floor is not an
achievable target. Full ladder and how to read it: `docs/nba/results.md`.

## Communication preferences (IMPORTANT)

**Always expand statistical acronyms on first use in any response, with a one-line plain-English
description.** The user has not taken a statistics class recently and has explicitly asked for
this. Do not assume familiarity with standard notation.

- MAE (mean absolute error — the average miss, ignoring whether it's high or low)
- RMSE (root mean squared error — like MAE but punishes big misses harder)
- SD (standard deviation — the typical distance from the average)
- R² (r-squared — the share of variation the model explains, 0 to 1)

This applies to output printed by scripts too, not just chat responses. Reports should carry their
own legend.

## Decisions made (and why)

| Decision | Choice | Rationale |
|---|---|---|
| Player impact metric | **Compute our own** from nba_api | Full 21-season point-in-time coverage, no licensing constraints, we control era normalization. |
| Fit / coaching modules | **Strict backtest gate, but report findings either way** | A module that fails its gate is dropped from the projection and written up as a finding. |
| Timing scope | **Preseason projection is the shipped default** | Matches how win-total markets are priced. A separate in-season model (own gate) exists for mid-season re-runs. |
| Modeling target | **Win percentage**, not wins | Three seasons in the window are not 82 games. Wins are a presentation-layer conversion. |
| Franchise join key | **`TEAM_ID`** | Stable across relocations and renames. Never join on name or abbreviation. |
| Market data role | **Strictly downstream, never a feature** | If market prices touch training, the disagreement analysis becomes circular. |

## Architecture

```
A. Player talent projection   (impact per 100 possessions, aging, shrinkage; off/def decoupled)
B. Minutes & availability     (240 min/game budget, injuries, replacement level)
C. Team aggregation           (minute-weighted sum -> separate off and def ratings)
D. One-year carryover         (+ rho * last-season residual; error persists ~1 season)
E. Monte Carlo season sim     (real schedule, home court, rest -> win distribution)
F. Market comparison          (downstream only)
```

- **Why bottom-up:** 21 seasons × 30 teams = 630 team-seasons — too few to fit a feature-rich team
  model. The heavy lifting happens at the player level (~10,600 player-seasons) and layer C stays
  nearly parameter-free. "Fit" is not a feature set (impact metrics already contain it); it was
  tested as residual structure and rejected.
- **Offense and defense are decoupled** (`decouple=True` in `project_team_ratings`): each is
  projected, aged, and calibrated with its own slope; net = off + def.
- **Defense is the weakest link.** Box defense was once ~60% rebounds-plus-blocks (it measured
  "being a center"). Fixed as far as it goes by position-relative rebounding, rim/hustle tracking,
  and a turnover-weighted box/RAPM (regularized adjusted plus-minus, a player-impact estimate from
  lineup data) blend; five further directions were neutral or negative, so the metric sits at a
  local optimum. `docs/nba/defense-rapm.md`.
- **Offense** uses position-relative offensive rebounding plus a RAPM offense blend weighted by
  prior-season top-scorer share. `docs/nba/offense.md`.
- **The one-year carryover** (`nbaproj/carryover.py`, rho ≈ 0.36 of last season's residual,
  +0.35 wins) is the one change that both beat the gate and survived adversarial verification.
  `docs/nba/findings.md`.
- Full design and staged plan: `DESIGN.md`.

## Layout

```
core/httpcache.py  throttled, disk-cached HTTP client shared across sports
nbaproj/
  cache.py        throttled, retrying, disk-cached nba_api wrapper
  ingest.py       per-dataset pulls; availability windows as constants
  teams.py        team-season target table; franchise spine
  baselines.py    walk-forward naive baselines + noise floor
  impact.py       box-score player impact (+ tracking, position-relative features)
  project.py      player → team projection; roster_opening_day for the backtest
  rapm.py / rapm_blend.py / bulk_pbp.py   RAPM from play-by-play and the box/RAPM blends
  carryover.py    one-year residual carryover
  simulate.py     Monte Carlo season simulation, rating sigma
  odds.py         historical preseason win totals scraper + strict franchise join
  market_live.py  LIVE Kalshi win-total ladders -> implied distribution (downstream only)
  rosters.py      draft priors + rookie projection; known-absence AND injury-return overrides
  inseason.py     rest-of-season (in-season) model core: SRS state at game N, w(N) shrinkage
nhl/              the NHL system (see nhl/DESIGN.md)
scripts/
  fetch_all.py / fetch_current.py   historical pull / live 2026-27 rosters (both cached)
  fetch_market.py    pull live Kalshi lines -> data/processed/market_2026_27.json
  project_current.py upcoming-season live bundle -> data/processed/projections_current.json
  build_snapshots.py historical bundles + combined snapshots.json
  build_rapm.py      canonical RAPM generator (box-informed prior on the CURRENT box metric)
  log_projection.py  append a run to data/projection_history.json (drift time-series)
  injury_return_candidates.py  data-only shortlist for injury_returns.json
  gate_*.py / precheck_*.py    walk-forward gates and pre-checks for each model change
  build_nba_players_ui.py      league-wide player leaderboard page
ui/               self-contained pages (template.html -> built .html); deployed to Vercel
docs/nba/         on-demand NBA history and reference (index below)
```

Override files (hand-authored, tracked in git; `data/overrides/` is NOT gitignored):
`data/overrides/known_absences.json` (players expected to miss time) and
`data/overrides/injury_returns.json` (its inverse — returning-from-injury stars).
`data/projection_history.json` is also tracked. `data/raw/` and `data/processed/` are gitignored
(regenerate with `python scripts/fetch_all.py`).

## Rebuild pipeline (live NBA bundle)

Use the virtualenv (`source .venv/bin/activate`) — the system Python lacks scikit-learn.

```
python scripts/fetch_current.py          # live rosters/coaches (cached; see the cache note below)
python scripts/fetch_market.py --refresh # live Kalshi win totals (downstream comparison)
python scripts/project_current.py        # upcoming-season live bundle
python scripts/log_projection.py         # drift history (idempotent per run date + model)
python scripts/build_snapshots.py        # historical bundles + combined snapshots.json
python ui/build.py                       # -> ui/projections.html + ui/performance/performance.html
python scripts/build_nba_players_ui.py   # -> ui/nba_players/players.html
```

Commit the rebuilt `ui/**/*.html` and `data/projection_history.json`; Vercel deploys `ui/` on
merge. `fetch_current.py` has no refresh flag: `nbaproj/cache.py` keys files as
`data/raw/{endpoint}__{sha1(params)[:12]}.parquet`, so delete the specific cache files to force a
re-pull. **A box-metric change means: regenerate `player_impact.parquet` → `build_rapm.py
--refresh` → rebuild bundles**, so the RAPM prior never goes stale.

## Critical integration boundaries

- **The market is strictly downstream.** Never use a market price as a model input or tuning
  target.
- **Overrides are live-bundle only.** `data/overrides/*.json` are manual, forward-looking judgment
  applied in `project_current.py`; they never touch the walk-forward backtest, so they need no gate.
  `injury_returns.json` *sets* availability (a raise); `known_absences.json` floors it via `min`;
  a return entry wins over an absence entry. Edit these files textually — they keep literals like
  `0.90`, and a `json.dump` round-trip reformats the whole file. Details: `docs/nba/overrides.md`.
- **Client-side recomputes must match Python exactly.** `ui/template.html`'s
  `computeRating`/`winParts`/`winsAt` and `scripts/build_nba_players_ui.py`'s `win_parts` mirror
  the server aggregation; re-verify after any aggregation change.
- **League wins are zero-sum** (~1230). Restoring players on one team pushes every other team down
  slightly; the what-if editor and per-method views are partial-equilibrium approximations.
- **Page caveats stay.** The page reads the shipped MAE from `CURRENT_MAE` in `ui/template.html`
  — bump it whenever the shipped number changes.
- **Basketball-Reference:** only `/leagues/` and the `/players/<letter>/` index are allowed;
  honor `Crawl-delay: 3` with a browser User-Agent; get anything else from nba_api.
  `odds._fetch_page` caches whatever it gets, including an empty page (the 2026-27 odds page is
  currently an empty shell — re-check with `refresh=True`).
- **Season-length landmines:** 2011-12 = 66 games, 2020-21 = 72, 2019-20 varied by team (market
  set for 82, so it is reported separately), 2012-13 BOS/IND = 81. `docs/nba/data.md`.
- **Deployment:** Vercel serves only `ui/` (Root Directory = `ui`), so `data/`, `nbaproj/` and
  `scripts/` are never uploaded. Routes live in `ui/vercel.json`; `/performance` is deliberately
  unlinked from the nav. `ui/DEPLOY.md`.
- **UI conventions (every page, every league):** the shared `:root` design tokens and fonts, a
  1080px content column, the `◐ theme` toggle on the shared `wincurve-theme` localStorage key, the
  grouped sticky top nav (current link `class="nl active"`), and the `?team=ABBR` cross-page deep
  link. Copy the exact markup and CSS from "UI conventions" in `docs/nba/ui.md` for a new page.

## General engineering rules

- Walk-forward backtesting throughout. Any coefficient is refit **per fold** using only prior
  seasons. Never fit on the full history and report it as out-of-sample.
- **Point-in-time correctness:** features for season N may use only information available before
  season N started. This is the easiest way to fake a good backtest.
- Normalize per-100-possessions and z-score **within season** — the league changed enormously
  across the window (pace ~90 → ~100 possessions, three-point attempts ~16 → ~38 per game).
- Every new stage must beat mean-reverted previous wins on walk-forward MAE, or it does not ship.
  Report the comparison explicitly.
- Fail loudly on data joins. `odds.py` raises if any season fails to match all 30 teams; prefer
  that to a silent partial join.
- **Tune against end-to-end win MAE in the real simulation gate**, never an internal diagnostic —
  the project has paid for this three times (ridge alpha, shrinkage, the blend-weight proxy).
- Fit a calibration on the same kind of quantity you apply it to.
- A result that beats a known-good baseline by a wide margin is a bug report, not a success.
- Averages are the enemy: replacing team-specific information with a league or career average has
  failed repeatedly.
- **Check `docs/nba/negative-results.md` before proposing a model change.** Many plausible ideas
  already failed; the common shape is "real at the player level, dies at the team-win number."
- Record the snapshot date with any projection output.

## Verification

There is no unit-test suite; verification is the walk-forward gate plus parity checks.

| Change | Check |
|---|---|
| Model / projection logic | the matching `scripts/gate_*.py` (5000 sims, paired seeds): report MAE, SE, and folds improved vs the shipped 7.39 and the 8.13 bar |
| Aggregation or UI recompute | `computeRating` reproduces every team's emitted rating; the JS `winParts` matches the Python port to ~1e-6 |
| Live rebuild | league wins sum to ~1230; the per-method sum-of-wins sanity print looks sane |
| Override edit | `git diff` shows only the intended lines; the player shows the `back` badge with the intended availability and minutes |
| Kalshi pull | all 30 teams present, no non-monotonic ladders |

## Open items (⬜)

Details: `docs/nba/roadmap.md`; finished items and stage history: `docs/nba/shipped.md`.

- Small-sample `prior_mpg` inflates camp signings' roles — shrink toward the bench default by games
  played; changes minute allocation, so it must clear the gate.
- Newcomers carry last season's role to a deeper team — the obvious fix ("canonical curve for
  newcomers") was already rejected; better served by the robustness readout.
- Disagreement robustness readout — show the gap with carryover zeroed, newcomers at bench
  minutes, and per method arm in `disagreementBlock` (display only).
- bbref 2026-27 Vegas page is up but empty — re-check, then wire as a second live line (copy
  `nhl/market_vegas.py`).
- If Porziņģis's absence runs long, lower his `injury_returns.json` availability further.
- Unsourced data: historical injury reasons; contract/salary history (for the contract-year test).
- Stage 7 coaching is blocked: `team_coaches` is mis-dated by one season at changes.

## Authoritative references

| File | Covers |
|---|---|
| `DESIGN.md` | full architecture, statistical traps, staged plan |
| `nhl/DESIGN.md` | NHL status, shipped model, pipeline, traps, open items (authoritative) |
| `docs/nhl/*.md` | NHL stage write-ups: data and impact (0–3b), team model (4–5), live and market (6) |
| `docs/nba/results.md` | backtest MAE ladder, baselines, how to read the noise floor |
| `docs/nba/data.md` | data inventory, availability windows, sources and scraping rules, season-length landmines, play-by-play schemas |
| `docs/nba/ui.md` | every NBA page, the UI conventions (nav markup/CSS), market ring, conviction, method toggle, what-if, leaderboard, public-detail decision |
| `docs/nba/overrides.md` | offseason-movement handling, `injury_returns.json` / `known_absences.json` mechanics and review tables |
| `docs/nba/defense-rapm.md` | defensive metric and RAPM history (what shipped, what was rejected, and why) |
| `docs/nba/offense.md` | offensive metric history (oreb, RAPM offense blend weight) |
| `docs/nba/negative-results.md` | ideas that failed their gate — do not re-attempt blind |
| `docs/nba/findings.md` | carryover, franchise effect, interval calibration, absence absorption, tanking correction, silent-bug lessons |
| `docs/nba/roadmap.md` | stage status table, open items, hypotheses to test |
| `docs/nba/shipped.md` | full stage history and completed roadmap items |
| `docs/nba_impact_methodology.md` | external talent-evaluator review of the impact methodology |
| `ui/DEPLOY.md` | Vercel deployment steps |
