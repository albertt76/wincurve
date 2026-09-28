# NBA — completed roadmap items (history)

> Moved verbatim from `docs/nba/roadmap.md` on 2026-09-28 so the roadmap stays short. Open
> follow-ups embedded in these write-ups are tracked in `docs/nba/roadmap.md`.

## Stage history

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
  **built**. Minute allocation is now validated end-to-end through Stages 4–5 (every change
  gates on team win MAE), but two known minute-allocation gaps remain open: small-sample
  `prior_mpg` and newcomers carrying last season's role to a deeper team (see `docs/nba/roadmap.md`).
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
  (bbref) page is up but still empty as of 2026-09-28 (see `docs/nba/roadmap.md`); Polymarket has no
  per-team win-total market. Still to do:
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

## Resolved decisions

- ✅ **Defense estimation — RESOLVED.** The once-deferred RAPM (regularized adjusted
  plus/minus) build was done — bulk play-by-play for 2013-14 → 2025-26 (`nbaproj/bulk_pbp.py`,
  `nbaproj/rapm.py`, `scripts/build_rapm.py`) — and shipped into the projection as the
  turnover-weighted defensive blend, then (2026-08-06) an offensive blend. Five further
  defensive experiments since then all came back neutral-to-negative: the defensive metric sits
  at a local optimum (see "Defensive-metric experiments — ALL FIVE RESOLVED").

## Completed open items

- ✅ **Test narrower position-relative-offense variants — RESOLVED, SHIPPED `oreb_p100` (2026-08-07).**
  The answer was not `fta_p100` (a near-no-op, +0.005) but `oreb_p100` — the offensive twin of the
  shipped defensive `dreb_p100`, which beats `ts_pct` cleanly (pure-box +0.132/6-6 all seeds vs
  +0.064/3-6; full-pipeline +0.051/6-6 where `ts_pct` is a null) and narrows the center/guard gap
  without overcorrecting while preserving Jokić-type stars. `ts_pct` was superseded (redundant with
  the RAPM offense blend). See the "oreb offense + usage-weighted RAPM offense blend (2026-08-07)"
  write-up above; the follow-up (`fta_p100` and other narrow variants) is closed.
- ✅ **Re-examine the RAPM offensive blend weight for high-usage/low-turnover stars — RESOLVED,
  SWITCHED to top-scorer share (2026-08-07).** The re-check confirmed the mechanism: keying the
  OFFENSE weight to prior-season top-scorer share (usage) instead of roster turnover (continuity)
  roughly doubles the blend weight for stable star-led teams (Brunson 0.11 → 0.20, Curry
  0.045 → 0.12, SGA 0.13 → 0.22) and restores each to ~box-level offense (erasing the ~1-win loss
  the turnover weight imposed), at ~zero aggregate cost (turnover 7.422 vs tss 7.436, within noise;
  honest prior-season ≈ contemporaneous). Shipped as `nbaproj.rapm_blend.offense_blend_weight`
  (defense stays on turnover). See the 2026-08-07 write-up above. Historical spot-check that
  motivated it: the flagged guards Trae Young (18% turnover) and Ja Morant (24%) gained offensive
  wins under the old turnover blend (+0.13, +0.23) while Brunson/Curry/SGA LOST (−0.93/−0.87/−1.23)
  despite equal-or-larger box-vs-RAPM gaps — exactly the usage-vs-continuity mismatch now fixed.
- ✅ **Injury-return / known-absence overrides reviewed — APPLIED 2026-09-28.** The owner accepted
  the shortlist's suggested settings (Giannis, Braun, Garland, Herro clean; Davis, Morant, Curry,
  LaVine, Embiid eased; Porziņģis revised 72% → 50%; Fox added as a known absence); results are in
  the "Second review (2026-09-28)" table in the injury-return section. The shortlist as reviewed:
  (found 2026-09-28)
  `known_absences.json` was last edited 2026-07-31 and `injury_returns.json` 2026-08-02, while
  `scripts/injury_return_candidates.py` surfaces **26 unreviewed candidates** (8 already in the
  file). Both files are manual user judgment by design (chronic absence vs clean return is a call,
  not a computation). **Per-player effect on team wins** — the real `project_current.py`
  pipeline run with every candidate restored through the override path (re-projected from the
  healthy basis season with normal aging + shrinkage), then attributed one player at a time with
  the exact client what-if port (others held fixed). *clean* = 85% availability, full basis
  minutes; *eased* = 70%, 90% minutes:

  | Player (team, age) | GP 22/23/24/25 | clean | eased | context (2026-09-28) |
  |---|---|---|---|---|
  | Giannis Antetokounmpo (MIA, 32) | 63/73/67/36 | +3.0 | +0.9 | "very, very good place" at media day |
  | Anthony Davis (WAS, 34) | 56/76/51/20 | +2.9 | +1.5 | GM: enters season fully healthy |
  | Ja Morant (POR, 27) | 61/9/50/20 | +1.8 | +1.2 | coach: healthy; a scout: "bad knee" |
  | Stephen Curry (GSW, 39) | 56/74/70/43 | +1.4 | +0.4 | says fine; seen limping mid-Sept |
  | Zach LaVine (SAC, 32) | 77/25/74/39 | +1.4 | +1.0 | no clear current news |
  | Joel Embiid (PHI, 33) | 66/39/19/38 | +1.4 | +0.4 | chronic knee |
  | Christian Braun (DEN, 26) | 76/82/79/44 | +1.2 | +0.7 | durable history; no news |
  | Darius Garland (LAC, 27) | 69/57/75/45 | +1.2 | +0.9 | no news |
  | Tyler Herro (MIL, 27) | 67/42/77/33 | +1.0 | +0.4 | no news |

  The other 17 move their team by under 1 win. **Do NOT add:** Zach Edey (−1.1), Kevin Porter Jr.
  (−0.9), Jeremy Sochan (−0.8), Larry Nance Jr. (−0.7) — restoring them LOWERS their team (the
  current projection beats their healthy-season basis, or the restored role is too big) — and
  **Jimmy Butler, who is a known absence, not a return** (ACL rehab, targeting Jan/Feb 2027 —
  already in `known_absences.json` at 44 games, which is what produces his 46% availability). **Existing entry to revisit:** Porziņģis
  (in the file at 72%) is out indefinitely to open camp with an unspecified health issue
  (reported 2026-09-28); 72% → 50% costs GSW 0.8 wins. **Known-absence add:** Fox
  (SAS, hamstring, targeting Nov 1 — ~0 wins by what-if). ⬜ **Follow-up:** if Porziņģis's camp
  absence turns out to be long, add him to `known_absences.json` too (the return entry wins over an
  absence entry, so lower its `expected_availability` as well).
  Accepting many at once dilutes each a little, because opponents improve too (all 26 together
  move WAS +2.0, not +2.9).
- ✅ Live 2026-27 market comparison shipped via **Kalshi** (`market_live.py`). Polymarket has no
  per-team win-total market. ⬜ **bbref Vegas: page is UP but EMPTY as of 2026-09-28** — no
  longer a 404, but only a header row (`Team | Odds`, no win totals), so `odds._parse_page` returns
  an empty frame. **Trap:** `odds._fetch_page` caches whatever it gets, so checking the upcoming
  season without `refresh=True` after that will keep reading the empty skeleton; re-check with
  `_fetch_page(2027, refresh=True)` and delete `data/raw/odds_html/NBA_2027_preseason_odds.html`
  if it's still empty. Once populated, wire it as a second live line (the NHL side's
  `nhl/market_vegas.py` source-aware ring is the pattern to copy).
- ✅ **"Bloated summer rosters" investigated and rejected** — not a defect; trimming fails
  the gate and the flagged gaps are real (defensive-metric) disagreements. See the
  INVESTIGATED & RESOLVED note and the negative-results table.
- ✅ **Roster definition for backtest — DONE.** `nbaproj.project.roster_opening_day`
  reconstructs opening-day rosters (the season roster minus mid-season arrivals, keeping
  zero-minute players such as a star hurt on opening night), and
  `project_team_ratings(mode="roster")` uses it — roster mode is the honest headline backtest.
  Limited to 2016-17+ by the `team_rosters` pull (see the historical-roster note under the Track
  record item).
- ✅ **Historical market lines in the per-season UI — SHIPPED (2026-07-31).** Each completed
  season's view now carries the **preseason Vegas over/under** as the hollow blue ring beside our
  mean and the actual diamond — a three-way per-team read (model vs market vs reality).
  `build_snapshots._attach_historical_market` joins `market_baseline.parquet` (`market_wins_82`,
  already 82-game-normalized) by TEAM_ID, emits `mkt_wins` + a per-snapshot `market_source`
  (`bbref:vegas_ou`). Historical is **Vegas-only** — Kalshi/Polymarket have no history. The UI
  marker/readout are now source-aware (`marketLabel`); the detailed live-Kalshi caveat stays
  upcoming-season-only, the hindsight caveat explains the Vegas ring.
- ✅ **Model-vs-market-vs-actual "Track record" view — SHIPPED (2026-07-31), extended to 7 seasons
  (2026-08-02), MOVED to the Performance page (2026-08-07).** Charts, per completed season, our
  MAE vs the Vegas MAE vs a .500 baseline, plus the honest scoreboard (**7 full seasons: model
  7.56, Vegas 6.90, model closer in 2 of 7**) and the per-team "best calls / where the market
  won." Extended from 5 to 7 by adding the two remaining FULL seasons in the backtest window
  (2017-18, 2018-19); the shortened 2019-20/2020-21 are skipped (bubble/covid, market not
  comparable). All client-side from the inlined snapshots (`renderTrackRecord`), no new data.
  **No longer a tab on the Records page — it now lives on the unlinked `/performance` page** (see
  "Records page controls + Performance page"). Further back is blocked on the `team_rosters`
  2016-17 floor (Vegas lines go to 2005-06) — a historical `commonteamroster` pull would unblock
  the full ~19 seasons.
- ✅ **Projection time series + drift charts — SHIPPED (2026-08-02).** `scripts/log_projection.py`
  appends each run of `project_current.py` (preseason) or `project_inseason.py` (in-season) to
  **`data/projection_history.json`** — a compact per-team record keyed by (run_date, model, season),
  idempotent per key. It lives at the `data/` root (**tracked**, not under gitignored
  `data/processed/`), so the series persists across checkouts/deploys, like the override files.
  `build_snapshots.py` inlines it (`projection_history`) into `snapshots.json`. A **Drift** view
  (`renderDrift`) charts, per team, projected wins over run dates as small-multiple SVG sparklines
  (latest wins + change-since-first chip), sorted by latest wins, shared y-axis. Client-side from the
  inlined history. **As of 2026-08-07 it lives on the unlinked `/performance` page** (with Track
  record), not as a tab on the Records page. **Honest scope caveat surfaced in
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
- ✅ **Auth / freemium access — built (2026-08-02), then REMOVED: team detail is PUBLIC (2026-08-07).**
  History: the detail was briefly gated by a Vercel serverless function + shared password
  (`ui/api/premium.js`, `PREMIUM_PASSWORD`, `x-unlock-password`) — chosen from four options (soft
  reveal / static-encrypted / **serverless+password** / managed-auth+Stripe) for real
  server-enforced gating with a clean runway to per-user auth. The "Unlock details" button was
  switched off 2026-08-06, then on **2026-08-07** the user asked to show details without login, so
  the gate was removed entirely: `ui/build.py` now inlines the full payload (players + what-if grid)
  publicly and `ui/api/premium.js` was deleted. See "Team detail is PUBLIC" in the UI section for
  the current state and how to re-gate.
  ⬜ **If a paid tier is ever wanted** (would require re-introducing a gate): restore the
  public/premium split + serverless function from git history, then swap the shared-password check
  for **per-user auth** (Clerk / Supabase / Auth0 — a session token) + **Stripe** billing. The
  serverless function is the seam. **Payment/Stripe must be wired by the user** — handling payment
  credentials is out of scope for the assistant.
