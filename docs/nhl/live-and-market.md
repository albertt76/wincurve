# NHL — live projection, Records page, and market comparison (Stage 6)

> Moved verbatim from `nhl/DESIGN.md` on 2026-09-28 so it is read on demand. The current
> status, shipped model, traps, and open items live in `nhl/DESIGN.md`; "above" / "below"
> may now point to a sibling file in `docs/nhl/`.

- 🟡 **Stage 6 — the live projection + market comparison + NHL "Records" page (in progress).** The
  upcoming-season deliverable: a live projected-standings page and the downstream market comparison.
  - **✅ Live projection (`nhl/season.py`, `rosters.live_roster`/`live_toi`,
    `scripts/nhl_project_current.py`).** `nhl/season.py` is the reusable Stage 5/6 pipeline (panel,
    strength→goals calibration, season-sim mean, one-year carryover, interval sigma) -- **verified to
    reproduce the gate's 10.50 exactly**, so backtest and production cannot drift. The UPCOMING season
    has no shift charts, so the roster comes from the NHL web API (`ingest.roster`, the hockey analog
    of the NBA `commonteamroster` snapshot), weighted by each skater's prior-season 5v5 TOI. The
    script runs the shipped pipeline on the current roster → per-team 2026-27 projected points + a
    calibrated 80% interval, recording the roster snapshot date → `data/nhl/processed/
    projection_current.json`. Face-valid (COL/CAR/TBL/MIN top, VAN/CGY/CHI bottom; the carryover
    carries the extremes -- COL +11 after a 121-pt season, VAN −13 after collapsing to 58); the league
    points sum ~2934 matches the structural ~2926. Also outputs projected **wins** (the sim's W), so
    the bundle is comparison-ready for a wins-settled market.
  - **✅ Market feed (`nhl/market_live.py`), downstream-only.** Kalshi **`KXNHLWINS`** -- the hockey
    analog of the NBA's `KXNBAWINS` -- per-team season **win** totals as a threshold ladder,
    reconstructed to an implied win distribution (median / mean / p10 / p90) with the identical proven
    NBA algorithm. NOTE the target: Kalshi settles on **wins**, not standings **points** (a win is 2
    points but an OT/SO loss is still 1), so the comparison is to projected *wins*, never points. As of
    the 2026-27 pre-season the series has **no open events** (win markets post closer to opening night
    -- the NHL echo of bbref's Vegas 404 for the NBA), so `market_win_table` returns `{}` and the page
    shows no ring; it attaches automatically once the market opens.
  - **✅ The NHL "Records" page (`ui/nhl_records/`, `scripts/nhl_build_records_ui.py`).** A
    self-contained page following the shared UI conventions (design tokens, the grouped nav with a new
    **NHL → Records** link added to *every* page, the shared `wincurve-theme` toggle, Vercel route
    `/nhl/records`): 32 teams on a **shared points axis** with the projected mean + 80% interval band,
    sortable by points / offense / defense / carryover, each row expanding to its off/def/carryover
    drivers and a plain-English disagreement story. The market ring is absent-but-ready (an honest "not
    posted yet" note). Consumes `projection_current.json` inlined via a `__DATA__` placeholder. This is
    the per-team disagreement surface the whole project builds toward, now for hockey.
  - **✅ Wins-vs-points reconciliation DONE (found + fixed proactively).** Testing the dormant market
    ring with a fabricated Kalshi post surfaced two bugs before the market ever went live: (1) the
    build script wrote `mkt_wins`, the template read `mkt` -- the ring would never have rendered; (2)
    `wins` was computed pre-carryover while `proj` (points) included it, so a large-carryover team
    (VAN, carry −12.9) produced a structurally impossible **negative implied OT-loss count**. Fixed by
    splitting the carryover onto wins at "2 points per marginal win" (`wins = wins0 + carry/2`), which
    makes `proj − 2·wins` recover exactly the sim's own OT-loss games (≥ 0 by construction; verified 0
    of 32 teams negative). The ring now converts Kalshi's win mean to a points-equivalent via each
    team's own implied OT-loss share (`mkt = 2·mkt_wins + our_otl`) so it plots correctly on the
    points axis once `KXNHLWINS` actually posts; the raw win number stays visible in the tooltip +
    a `mkt Xw · ±Y` readout (the NBA `mkt N · ±diff` pattern).
  - **✅ Per-team player-level roster detail DONE (`nhl_project_current.roster_detail`).** The actual
    "here is the structural reason why" deliverable: every team's expanded panel on the Records page
    now lists its **top 6 roster contributors**, ranked by **share of the team's net rating**
    (`net_impact × his TOI share of the team total`) -- a decomposition that sums exactly to the
    team's aggregate net by construction (mirrors the NBA project's per-player win-value
    reconciliation; verified 0.0000 error against `aggregate.team_ratings`' own net for every team).
    Name + position from the live roster / MoneyPuck, sign-colored off/def, a centered contribution
    bar (reusing the NHL Players page's bar convention). Rookies/uncovered players correctly fall to
    replacement level rather than showing blank. Face-valid: Colorado's #1 contributor is Nathan
    MacKinnon (+0.449 off), its defense dragged by Juulsen/Kulak -- exactly the eye test.
  - **✅ What-if roster editor DONE.** The NHL analog of the NBA Records page's live-recompute
    feature: every team's expanded panel now has an "Edit roster" table (all rostered skaters, not
    just the top 6) with a per-player **bench** toggle -- benching swaps his off/def to replacement
    level at his own ice time (an "injury / departure" scenario; the carryover stays fixed, since it
    reflects last season, not this edit). The recompute is an **exact client-side JS port** of
    `nhl.gamesim` + `nhl.season.goal_rates` (Poisson win probability, the OT/SO branch, the trinomial
    points, `expected_points`/`expected_wins`) -- verified to reproduce the server's own numbers for
    all 32 teams with no edits applied (±0.02-0.11 pts of JSON-rounding noise, which the UI sidesteps
    by showing the exact server value whenever nothing is benched). Only `off_mean`/`def_mean` are
    frozen at build time (a single edited team barely moves the true 32-team average) -- everything
    else is live. The team's row on the shared axis (band + mean tick + readout) updates in place;
    the interval is re-centered on the new mean at its original width (season luck barely depends on
    *which* players make up a given team strength). Face-valid: benching Colorado's Nathan MacKinnon
    (its #1 contributor, ~half the team's net rating) drops the projection 108.1 → 102.3 (−5.8 pts).
    Not a trade editor (no swap-in-any-player pool) -- that is the natural v2 if wanted.
  - **✅ CORRECTION + Vegas market integration DONE.** The "historical track-record view stays
    blocked" note above was **wrong** -- it was based on checking Kalshi (genuinely empty) and
    hockey-reference's robots.txt in isolation, without actually searching for the page itself.
    Doing that search found **hockey-reference.com/leagues/NHL_&lt;year&gt;_preseason_odds.html**,
    the exact NHL analog of the bbref page the NBA project already relies on (same company, same
    `/leagues/` robots.txt allowance, same `data-stat` table convention) -- and it carries a
    **points** over/under (our model's exact target unit, not wins) with the real final result, for
    every season since **2010-11**, which happens to be the *exact* floor of our own RAPM/shift-
    chart backbone. So every season the model can ever backtest also has a real market line.
    - **`nhl/odds.py`** (mirrors `nbaproj/odds.py` closely): fetches + caches the 16 available
      pages (`Crawl-delay: 3`, browser User-Agent), parses `team_name`/`over_under`/`points` cells,
      joins onto `nhl.teams.target_table()`'s era-correct team names (one fix needed vs the NBA
      version: accent-folding "Montréal" -> "Montreal" via `unicodedata`, not just punctuation-
      stripping). Validated: the page's own `final_points` matches our `team_summary` points
      exactly for every row checked -- the parse is correct.
    - **`scripts/nhl_market_history_report.py`** -- the actual measurement, on the SAME walk-forward
      folds as the Stage 5 gate (not cherry-picked): **our model 10.50 vs the real Vegas line 10.47
      over 9 full-season folds (paired mean diff +0.03, SE 0.22, t=0.15) -- a clean statistical
      TIE**, closer to actual in 6/9 individual folds. This is a materially different honest read
      than the NBA project's (which does not beat its market, 7.58 vs 6.88) -- a from-scratch,
      publicly-sourced hockey model matching a professional sportsbook line almost exactly.
      (2020-21's Vegas MAE spikes to 29.1 -- even the sharp market badly missed that
      covid-compressed, realigned season; correctly excluded from the full-season headline.)
    - **`nhl/market_vegas.py`** -- the LIVE piece, answering "can we use Vegas while Kalshi is
      dormant": BetOnline's 2026-27 points line (opened 2026-07-20, found via a web search, since
      there is no stable per-season URL the way hockey-reference's archive has -- hand-curated in
      `LIVE_SOURCES`, the same discipline as `known_absences.json`, must be re-found each season).
      Verified as a real static HTML table (not JS-rendered), parsed and joined onto all 32 teams.
    - **The Records page market ring is now source-aware** (`scripts/nhl_build_records_ui.py`):
      Vegas points preferred (real units, no conversion, `mkt_source: "vegas"`) with Kalshi wins as
      a fallback for any team Vegas didn't cover (still converted via the OT-loss-share approach
      above, `mkt_source: "kalshi"`). Live now: **32/32 teams from Vegas**, mean disagreement 4.1
      points, biggest gaps FLA (we project 22.3 points BELOW the Vegas line -- our carryover and
      roster numbers see a more mid-pack team than the market's expectation) and PIT (+7.1 above).
  - **⬜ Remaining.** A "Track record" UI VIEW (the NBA Records page's Projections/Track-record
    toggle) is the natural next step for the historical Vegas comparison -- the report script above
    is the data/measurement, not yet a page. Also: injury / known-absence overlays the NBA project
    carries as hand-authored, forward-looking overrides (the what-if editor's bench toggle covers
    some of this, not the "player X returns from injury" inverse case).
