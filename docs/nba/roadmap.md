# NBA — roadmap and open items

Completed items and the full stage history: `docs/nba/shipped.md`. Update this file in the same
commit as related work; when an item ships, move its write-up to `shipped.md` and leave a line in
the status table.

## Stage status (2026-09-28)

Shipped backtest: **7.39 wins MAE** vs market 6.88; the bar is 8.13 (`docs/nba/results.md`).

| Stage | Status | Headline |
|---|---|---|
| 0 — data layer | ✅ | 21 seasons of cached, point-in-time pulls |
| 1 — baselines | ✅ | naive 8.07, market 6.67 (2013-14..2025-26 window) |
| 2 — player impact, aging, shrinkage | ✅ | beats "reuse last season" by 9.4% |
| 3 — availability, minutes, rosters | 🟡 | availability validated; two minute-allocation gaps open (below) |
| 4 — team aggregation | ✅ | calibration fitted on *projected* aggregates |
| 5 — Monte Carlo simulation | ✅ | nominal 80% interval covers 79.6% |
| 5b — one-year carryover | ✅ | 8.30 → 7.95 |
| 6 — "fit" as residual structure | ✅ rejected | null out-of-sample, sign opposite the hypothesis |
| 7 — coaching / continuity | 🟡 blocked | `team_coaches` is mis-dated by one season at changes; re-pull first |
| 8 — market comparison | ✅ | historical Vegas + live Kalshi; contract-year test still open |
| Offense/defense decouple | ✅ | MAE-neutral; unlocks the off/def split |
| RAPM blends (defense, then offense) | ✅ | defense 7.96 → 7.77; offense and oreb fixes → 7.39 |
| In-season (rest-of-season) model | ✅ | beats the carried-forward preseason at 25 and 50 games |

## Open items (⬜)

- ⬜ **Small-sample `prior_mpg` inflates camp signings' roles (found 2026-09-28).**
  `project_current.py` sets each player's projected role to last season's raw minutes per game
  (`prev.m / prev.g`) with no discount for games played, so a 1-4 game late-season cameo on a
  tanking team becomes a starter-sized role on the new team: Hayden Gray (1 GP → 25.0 mpg, BOS),
  Lawson Lovering (2 GP → 25.0, MEM), Kadary Richmond (3 GP → 22.0, CLE — why CLE barely moved
  after adding Peyton Watson), Keshon Gilbert (4 GP → 18.5, ATL), all at replacement-level impact.
  Genuine injury-shortened seasons (Bradley Beal 6 GP, Dereck Lively 7 GP) take the same code path
  but land near a sensible role. Candidate fix: shrink `prior_mpg` toward the 8.0 bench default by
  games played (e.g. weight `g/(g+k)`), sparing `injury_returns.json` players. It changes minute
  allocation, so it must clear the walk-forward gate before shipping — NOT applied in the
  2026-09-28 data refresh.
- ⬜ **Newcomers carry last season's role to a deeper team (found 2026-09-28).** The broader,
  full-sample twin of the item above: a player's projected minutes are last season's mpg wherever
  he now plays, so a heavy-minutes role on a tanking team carries straight into a deep rotation.
  Cody Williams (24.3 mpg on UTA, −3.57 impact) is projected at 24.3 mpg on MIN behind Edwards /
  LaMelo Ball / Kuminga / Dosunmu; at a realistic 10 mpg MIN moves **43.3 → 46.5** and its High-
  conviction −5.8 gap vs Kalshi shrinks to −2.6. NYK's five offseason bench arrivals (Bruce Brown,
  Konchar, Agbaji, Wiseman, Eubanks) carry 237 → 291 supplied min/240 and drag it ~1.7 wins the
  same way. **Caution before "fixing" the projection:** the obvious fix was already gated and
  REJECTED — "canonical curve for newcomers only" (8.44 → 8.61, see the negative-results table),
  consistent with the recurring "averages are the enemy" lesson. So expect little aggregate payoff
  from a projection change; the per-team *credibility* payoff is real and is now surfaced per team by the
  robustness readout's newcomer check (shipped 2026-09-28, `docs/nba/ui.md`). (Measured with an exact Python port of the client `computeRating` +
  `winsAt`, reproducing all 30 shipped teams within 0.02 wins.)
- ⬜ Historical **injury reasons** still unsourced (Pro Sports Transactions needs a UA;
  otherwise only games-missed is available)
- ⬜ Contract/salary history unsourced (only needed for the contract-year test)
- ⬜ **bbref Vegas: page is UP but EMPTY as of 2026-09-28** — only a header row (`Team | Odds`),
  so `odds._parse_page` returns an empty frame. **Trap:** `odds._fetch_page` caches whatever it
  gets; re-check with `_fetch_page(2027, refresh=True)` and delete
  `data/raw/odds_html/NBA_2027_preseason_odds.html` if it's still empty. Once populated, wire it
  as a second live line (copy the NHL side's source-aware `nhl/market_vegas.py` ring).
- ⬜ **Porziņģis follow-up (2026-09-28 injury review).** If his camp absence turns out to be long,
  add him to `known_absences.json` too — and lower his `injury_returns.json`
  `expected_availability`, since a return entry wins over an absence entry.
- ⬜ **Contract-year hypothesis test** (Stage 8) — blocked on the salary data above.
- ⬜ **Stage 7 coaching** — re-pull `team_coaches` (mis-dated by one season at changes) before any
  further coaching work; the untested case is a coaching change.
- ⬜ **Offseason-move undo refinements** (need a transactions feed we don't have): true two-for-one
  trade *pairing*; in-season trades would fall out of diffing periodic roster snapshots once
  projection-history logging captures rosters, not just per-team projections.
- ⬜ **In-season v2 redemption path** (not built; narrow value): a post-deadline split using the
  *current* (post-trade) roster snapshot, restricted to actual deadline-trade teams. Machinery and
  gate kept (`scripts/gate_inseason_v2.py`).
- ⬜ **If a paid tier is ever wanted:** restore the public/premium split + serverless function from
  git history, then swap the shared password for per-user auth (Clerk / Supabase / Auth0) + Stripe.
  **Payment/Stripe must be wired by the user.** See "Team detail is PUBLIC" in `docs/nba/ui.md`.


## Decisions pending user input

*(None pending as of 2026-09-28.)*

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
