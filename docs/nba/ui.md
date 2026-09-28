# NBA — UI pages, conventions, and deployment

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

## UI

### UI conventions (all leagues)

Every wincurve web page — NBA projections (`ui/template.html` → `/`), NBA players
(`ui/nba_players/template.html` → `/players`), NHL impact (`ui/nhl/template.html` → `/nhl`), and
**every future league page** (NFL, soccer, …) — is a `template.html` → self-contained `.html`
build that MUST read as one product. Three rules make that happen; new pages follow them, they are
not optional.

**1. Shared design tokens + fonts.** Every template's `<style>` opens with the SAME `:root` design
tokens and font stacks — do not introduce new colors or fonts, reuse these (they are theme-aware:
a `@media (prefers-color-scheme: dark)` block plus `:root[data-theme="dark"]` / `[data-theme="light"]`
overrides so the per-page theme toggle wins):

```
--ground --surface --surface-2 --line --line-soft --ink --muted --faint   (+ per-page accents)
--mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
--sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
```

Monospace for all data/numbers, system sans for prose. The content column is `.wrap { max-width:
1080px }` on every page (unify to 1080, do not drift). Every page also carries the SAME `◐ theme`
toggle (`<button class="theme-btn" id="theme">` in its header) backed by a **shared
`wincurve-theme` `localStorage` key**, so the chosen light/dark theme persists as the user moves
between pages — a new page must reuse that exact key, not invent its own (the projections page was
the one page missing the toggle; fixed 2026-08-05, and the NHL/`nhl-theme` + players/`nba-players-theme`
keys were unified to `wincurve-theme` at the same time).

**2. Shared top nav bar (grouped by league).** Every page includes the SAME full-width sticky bar as
the first element of `<body>` (immediately before `<div class="wrap">`). Links are **grouped by
league** — a faint `nl-league` label (NBA / NHL, followed by a `▸` via `::after`) heads each
`nl-group`, and **the current page's link is given `class="nl active"`** (NBA projections →
"Records"; players → "Players"; NHL → "Players"; a new league adds its own `nl-group` with a label and
routes, marking its link active on its own page). The markup and CSS are identical across templates
and use only shared tokens (`--ink`/`--muted`/`--faint`/`--line`/`--surface`/`--mono`):

```html
<nav class="topnav"><div class="topnav-in">
  <a class="brand" href="/">wincurve</a>
  <span class="nl-group">
    <span class="nl-league">NBA</span>
    <a class="nl active" href="/">Records</a>
    <span class="nl-sep">|</span>
    <a class="nl" href="/players">Players</a>
  </span>
  <span class="nl-group">
    <span class="nl-league">NHL</span>
    <a class="nl" href="/nhl/records">Records</a>
    <span class="nl-sep">|</span>
    <a class="nl" href="/nhl">Players</a>
  </span>
</div></nav>
```
```css
.topnav { position: sticky; top: 0; z-index: 50; background: var(--surface); border-bottom: 1px solid var(--line); }
.topnav-in { max-width: 1080px; margin: 0 auto; padding: 9px 20px; display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
.topnav .brand { font-family: var(--mono); font-weight: 650; font-size: 14px; color: var(--ink); text-decoration: none; letter-spacing: -.01em; }
.topnav .nl { font-size: 13px; color: var(--muted); text-decoration: none; padding: 3px 1px; border-bottom: 2px solid transparent; }
.topnav .nl:hover { color: var(--ink); }
.topnav .nl.active { color: var(--ink); font-weight: 600; border-bottom-color: var(--ink); }
.topnav .nl-group { display: inline-flex; align-items: baseline; gap: 9px; }           /* league group */
.topnav .nl-league { font-family: var(--mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--faint); }  /* + ::after "▸" */
.topnav .nl-sep { color: var(--line); font-size: 12px; }                               /* "|" between same-league links */
```

Routes live in `ui/vercel.json` (`cleanUrls: true`): `/` → projections, `/players` → NBA players,
`/nhl` → NHL impact (Players), `/nhl/records` → NHL Records (the Stage 6 projected-standings page,
`ui/nhl_records/`), and `/performance` → the NBA **Performance** page (`ui/performance/`, Track
record + Drift — see below). A new league page adds its route there AND its nav link above.

**Deliberately UNLINKED route: `/performance`.** The Performance page is reachable by direct URL
only — it is intentionally **not** in any nav group (the top nav on that page carries the other
links but no self-link). So a route in `vercel.json` does **not** always imply a nav link; this one
is the exception, by owner request (keep the analytics/track-record surface off the main NBA page
but still accessible). If you add a nav link later, add it to every template's `.topnav`.

**3. Cross-page `?team=` deep-link.** Pages link to each other with a `?team=ABBR` query param; the
**source** page emits the link, the **target** page reads and applies it. NBA is the reference
implementation, and every league follows the pattern:
- **Target** (NBA players, `ui/nba_players/template.html`): on boot, read
  `new URLSearchParams(location.search).get("team")`, and if it matches a known team (case-insensitive,
  wrapped in try/catch) pre-select that team's filter and render filtered; an unknown/absent/malformed
  param falls back to showing all.
- **Source** (NBA projections, `ui/template.html`): `teamPlayersLink(t)` emits a small
  `<a class="teamlink" href="/players?team=${t.abbr}">ABBR players on the leaderboard →</a>` at the top
  of each team's expanded panel, styled with shared tokens. It is prepended in `renderPanel` (the
  panel body below it — roster, per-player Off/Def/≈Wins, what-if editor — is now public too; see
  "Team detail is PUBLIC" below).

A future league page that wants a per-team drill-down uses the same contract: its target page reads
`?team=`, its source page links with it.

**4. External player links (verified, not guessed).** Both league-wide leaderboards (NBA Players,
NHL Players) show a small **↗** after each player's name, opening his Basketball-Reference /
Hockey-Reference page in a new tab (`target="_blank" rel="noopener noreferrer"`). The URL is
looked up, never algorithmically guessed: `nbaproj/player_links.py` and `nhl/player_links.py` each
pull their site's own **A-Z player index** (`/players/<letter>/`, ~26 pages, cached, `Crawl-delay:
3`, the same politeness already established for `nbaproj/odds.py` / `nhl/odds.py`) — a full list of
every player ever with his exact slug, so the lookup is a verified exact match instead of the
well-known-but-occasionally-wrong "last5+first2+01" slug formula (a wrong guess would silently
link to the wrong player). Name normalization handles accents (`unicodedata` NFKD fold — "Dončić"
→ "doncic"), initials ("J.T." → "jt", not "j t"), and generational suffixes our data may carry
that the index doesn't ("Bobby Portis Jr." → "Bobby Portis"). A same-name collision is
disambiguated by active-years overlap with the player's season. Match rate: NBA ~99-100% for any
real rostered player (the ~20% aggregate miss is almost entirely very-recent draft prospects with
no bbref page yet, plus a pre-existing empty-name artifact in some historical snapshot rows —
verified, not a bug); NHL ~98%. `scripts/build_nba_players_ui.py --relink` patches links into an
already-built `players.html` without needing `snapshots.json` (for a checkout where that
gitignored intermediate isn't present); `scripts/nhl_build_impact_ui.py`'s normal build includes
links every time since the NHL parquets are checked out directly.

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
bbref Vegas over/unders are still unposted (404 through August; as of 2026-09-28 the page exists
but carries no lines yet — see `docs/nba/roadmap.md`), and Polymarket has no per-team
win-total market. Kalshi quotes a **threshold ladder** ("20+/25+/30+ wins"), so we
reconstruct a full market-implied distribution (median, mean, p10/p90) and compare
distribution-to-distribution, not just point-to-point. **Strictly downstream — the market
never touches the model** (that rule is what keeps the disagreement analysis meaningful).
Mean |ours − market| ≈ 4.2 wins; biggest gaps are the deliverable (open a team for the
roster reason). Refresh through the season with `fetch_market.py --refresh`.

**Bug found + fixed (2026-08-05): ladder reconstruction used a forward-only monotonicity
clamp.** User-reported: the live page showed Indiana's Kalshi win total as 38.9 when the real
market implied ~43.5-44. Root cause in `nbaproj.market_live._ladder`: a threshold ladder's
survival function P(wins ≥ k) must be non-increasing in k, and the old fix for noisy rungs
clamped every LATER point down to match an earlier noisy one. One illiquid rung (IND's "10+
wins" quote had a $0.59/$0.99 bid/ask — mid $0.79 — sitting below five tighter, more-liquid
quotes above it at ~$0.92-$0.955) dragged all five down to its own value, corrupting the mean
by ~5 wins and the implied 10th-percentile down to 7.4 wins (nonsensical for a playoff-caliber
roster). **Not just staleness** — a sweep of the live ladder found 20/30 teams currently have at
least one monotonicity-violated rung, so this was a live, recurring risk, not a one-off. Fixed
with an **isotonic regression** (`sklearn.isotonic.IsotonicRegression`, already a project
dependency) across all rungs at once, weighted by each rung's own liquidity (inverse bid/ask
spread) — a tight two-sided quote pulls the fit toward itself, a wide/one-sided one is trusted
less and gets pooled with neighbors instead of dragging them down. Verified against the exact
stale IND snapshot that triggered the report (38.9 → 42.8 mean, p10 7.4 → 31.9) and against a
fresh live pull (0/30 teams non-monotonic, all distributions valid).

**Historical market comparison (completed seasons, shipped 2026-07-31).** Each completed
season's view *also* carries a market ring — the **preseason Vegas over/under** for that year
(bbref, `market_baseline.parquet`, joined by TEAM_ID in `build_snapshots._attach_historical_market`,
already 82-game-normalized). With our mean, the Vegas ring, and the actual diamond on one bar,
you read per team whether the model or the market landed closer. Historical is **Vegas-only** —
Kalshi/Polymarket are too recent to have any history. Marker/readout are source-aware
(`marketLabel` → "Kalshi" live, "Vegas" historical); the market is still strictly downstream.

**Track record view (shipped 2026-07-31, extended to 7 seasons 2026-08-02; MOVED to the
Performance page 2026-08-07).** Scores the model against the market longitudinally: per completed
season, our MAE vs the Vegas MAE vs a .500 baseline (bars), the honest aggregate (**7 full
seasons: model 7.56, Vegas 6.90, model closer in 2 of 7** — we do not beat a sharp market on
average, as promised), and the per-team **best calls / where the market won** (biggest
model-vs-Vegas disagreements ranked by who was closer to the actual). All computed
**client-side from the inlined snapshots** (`renderTrackRecord`,
`seasonModelMAE`/`seasonMarketMAE`/`seasonBaselineMAE`) — no new data. The per-team disagreement
payoff is the whole point of the tool, now made visible and gradeable. **It no longer lives on the
Records page** — it and Drift were moved to the standalone, deliberately-unlinked **Performance
page** (`ui/performance/`, route `/performance`); see "Records page controls + Performance page"
below.

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

**Disagreement robustness readout (shipped 2026-09-28).** A second layer under the conviction tag in
`disagreementBlock` (`robustnessChecks` / `robustnessBlock` in `ui/template.html`): the market gap
re-priced with **one weak assumption removed at a time** — the three moves every hand-run pressure
test used (2026-08-12, 2026-09-28 ×2):
- **without carryover** — the active method's rating minus `t.carryover`;
- **newcomers at bench** — offseason arrivals (`p.prev`) the model rates below replacement level
  (by the shipped Blended per-100 value, whatever the active method) yet projects above 10 mpg
  (`NEWCOMER_BENCH_MPG`), cut to 10 mpg via a new optional `mpgOverride` argument on
  `computeRating`; the minutes flow exactly as the model allocates any roster's, and the carryover
  stays. A first rule (cut any newcomer outside his team's top 8 by rating) was rejected while
  prototyping — it benched real rotation players (Randle, DeRozan, Vučević). Upcoming season only:
  historical snapshots carry no `prev`, so past seasons show just the other checks;
- **the other two method arms** (Blended / Box / RAPM).

Each chip is classed `holds` (same side, at least half the gap left), `fades` (under half, or
inside the 1.5-win agreement band — `AGREE_WINS`, now shared with the row readout), or `flips`
(at least 1.5 wins on the other side of the market), followed by a one-line verdict. It runs through
the same `computeRating`/`winsAt` as the what-if editor, so it follows roster edits. Verified
against an independent Python port (every check, all 30 teams, to rounding) and against the
pressure tests (MIN −6.1 → −3.0 with Cody Williams at bench minutes). Display only — no gate.

**The readout's own track record is shown on the page, and it is not flattering.**
`robustnessTrackRecord` swaps in each completed season's snapshot (then restores the live one),
classifies every Blended gap of 1.5+ wins against the preseason Vegas line, and counts how often
the model landed closer to the actual record. Measured 2026-09-28 over 7 seasons: **gaps that held
under every check 39% (n=87) vs gaps that weakened 47% (n=74)** — robustness does *not* pick
winners. Held gaps are bigger (mean 6.5 vs 3.8 wins), but within gap-size bands they did no better
(1.5–4 wins: equal; 4–7 and 7+: worse) and they did worse in 5 of 7 seasons. Conviction fared the
same (model closer on 45% of High, 42% of Medium, 42% of Low gaps). Each panel prints the live
figures, and the glossary says both tags describe *what a gap rests on*, not whether it is right.
First live read (2026-27): WAS −9.3 (High conviction) shrinks to −4.1 without the carryover, as do
IND −9.1 → −4.0 and CHA +8.3 → +1.5; MIN −6.1 → −3.0 and MIA −4.1 → +0.5 with newcomers at bench;
NYK, SAS and LAC hold under every check.

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

**Offense/defense split + defensive disagreement (added 2026-07; row-level `O·D` readout REMOVED
from the Records page 2026-08-07).** The team's offense/defense rating split is shown in its
**expanded panel head** (Offense / Defense stats), colored by sign, so you can see whether a
projection is carried or dragged by offense vs defense — e.g. Detroit is defense-carried,
Charlotte offense-carried. (Until 2026-08-07 each collapsed team ROW also carried a small
`O ±x · D ±y` readout in the Wins column via `odReadout()`; that was removed to declutter the
Wins column — it duplicated what the expansion already shows. `odReadout` is gone; the panel-head
split remains.) The roster panel splits Impact into **Off / Def**
columns and its header shows the team **Offense / Defense** rating. Players whose box-score
defense disagrees with play-by-play **RAPM** get a **`D↑` / `D↓` flag** by their name (`D↑` =
RAPM higher, we likely underrate his D — e.g. Alex Caruso, NAW; `D↓` = we likely overrate,
e.g. Dyson Daniels), hover for both numbers. RAPM is comparison-only, not in the projection.
This is fed by the decoupled projection: `project_current.py` / `build_snapshots.py` emit
per-team `off_rating`/`def_rating`, per-player `off`/`def`, and each player's most-recent
`box_def`/`rapm_def` (via `nbaproj.rapm.box_vs_rapm_by_player`, walk-forward). The client
recompute is decoupled too, so a roster edit reprices offense and defense independently.

**Method toggle: Blended / Box / RAPM (shipped 2026-08-06, replaces the old RAPM-only-defense
side readout; MOVED above the table 2026-08-07).** A 3-way pill switch controls **which
measurement drives every number on the page**: team ratings, win totals, bars, and rankings on
Records; Impact/Off/Def/≈Wins on Players. On the **Records page** it now sits in a **controls bar
above the table** (`.controls` → a `method` `.grp` of `.pill` buttons, next to the new
`filter team…` box — see "Records page controls" below), styled like the NHL Records
sort/filter controls, rather than in the header title-row where it used to live; on the Players
leaderboard it stays in that page's own header. The buttons keep their `#method-blended/box/rapm`
IDs and the `.active` class the JS toggles (`.pill.active` = teal fill). **Blended** (the shipped
default) is the turnover-weighted box+RAPM blend on both sides. **Box** is the classic
box-score-only method. **RAPM** prices both offense and defense purely from play-by-play, no box
score at all — the natural generalization of the old defense-only RAPM arm, now covering offense
too and selectable rather than always-on. Each method is calibrated with its **own slope**
(`nbaproj.rapm_blend.calibrate_blend` returns all three: `off_slope`/`def_slope` for Blended,
`off_slope_box`/`def_slope_box`, `off_slope_rapm`/`def_slope_rapm`) — reusing the blended slope on
an unblended aggregate would repeat the exact mismatch this project's calibration lessons warn
against.

No new Monte Carlo simulation needed: every team already carries a precomputed rating-offset grid
(`RATING_GRID`, ±12 in 1.5 steps) built by resimulating that team's own uncertainty around its
Blended rating; a Box or RAPM rating is computed with its own slope and **interpolated on that
same grid** (`_grid_interp` server-side, `winsAt` client-side) — exactly how the old RAPM-only
arm got `wins_rapm` with no extra simulation, now extended to give full win distributions (not
just a mean) for both new arms. **Known approximation, kept deliberately**: every other team is
held at its Blended rating while you look at one method (a partial-equilibrium view, not a full
league resimulation) — the same simplification the old single-arm readout always used, now
carrying more weight since it's the primary number for all 30 teams instead of one side stat.
Carryover and sigma are also shared unchanged across all three methods, matching that same
precedent. Confirmed empirically after shipping: max rating offset from Blended is ~2-3 points
(current season), well inside the ±12 grid, and each method's win total sums to roughly (not
exactly) 1230 across the league — box and RAPM were never checked for that zero-centering
property the way the shipped blend implicitly has been by living in production, so a per-method
sum-of-wins sanity print runs on every rebuild.

Emitted per-team with a flat suffix convention (unsuffixed = Blended, `_box`, `_rapm`):
`rating`/`off_rating`/`def_rating`/`wins`/`p10`.../ and their `_box`/`_rapm` twins. Emitted
per-player on the Players leaderboard the same way (`scripts/build_nba_players_ui.py`
`win_parts(..., method=)`): **this changed what unsuffixed `off`/`def`/`impact` mean** on that
page — before the toggle they were pure box; now unsuffixed = Blended (a derived rating,
replacement + the same off_dev/def_dev the wins calc uses), `_box` = the old meaning, `_rapm` =
raw RAPM. Client-side (`ui/template.html`'s `computeRating`/`winParts`, `ui/nba_players/
template.html`'s `mkey` field-resolution) mirrors the Python bit-for-bit, verified by direct
comparison. **Display only, never an input** — re-weighting the shipped blend toward RAPM did not
clear the gate (`scripts/gate_blend_weight.py`); this toggle is for inspection, not a model change.

Non-obvious finding surfaced while building this: whether RAPM-only helps or hurts a team's
*offense* wins tracks roster **turnover**, not the size of the player's box-vs-RAPM gap — a
stable-roster star (Curry, 4% turnover, box off +2.80 vs RAPM off +7.05, the biggest gap in the
league) barely moves toward his own higher RAPM number and can net *lower* under the blend once
the league-wide slope recalibration is accounted for, while a churned-roster player with a smaller
gap can net higher. See the `docs/nba/shipped.md` "Completed open items" entry for the follow-up (re-checking
`top_scorer_share_weight` specifically for this player group).


**Records page controls + Performance page (2026-08-07).** Four UI changes, all client/build-side
only (no model change), matching the NHL Records page so the two Records pages read as one product:

1. **Method toggle moved above the table.** The Blended/Box/RAPM switch left the header title-row
   for a `.controls` bar directly above the rows (a `method` `.grp` of `.pill` buttons), styled like
   the NHL Records sort pills. Same button IDs (`#method-blended/box/rapm`) and `.active`-class JS as
   before; only the position and pill styling changed.
2. **`filter team…` box.** A live substring filter (`#team-filter` → `TEAM_QUERY`) in the same
   controls bar, filtering the projections table by team abbreviation OR full name (so "den" matches
   DEN *and* Gol**den** State, exactly like the NHL box). A `#team-count` ("N of 30") shows only while
   filtering. Display only — never touches the model.
3. **Wins column reflowed.** The per-row **`O ±x · D ±y`** readout (`odReadout`, now deleted) was
   removed from the Wins column — it duplicated the offense/defense split already in the team
   expansion. The column is widened (`grid-template-columns` third track 108→164px) and shows the
   NHL-style compact readout: bold mean on top, then the 80% range and the market ring/diff together
   on one line (`.line2`).
4. **Track record + Drift moved off the Records page to a new Performance page.** The Records page's
   3-way `Projections / Track record / Drift` tab bar is gone (only Projections remains, so the tab
   bar was removed entirely); `renderTrackRecord`/`renderDrift` and their helpers (`completedSeasons`,
   `seasonModelMAE`, `seasonBaselineMAE`, `BASELINE_WINS`, the 3-way `setView`) moved to
   `ui/performance/template.html` → `ui/performance/performance.html`, route **`/performance`**, with
   its own 2-way Track record / Drift toggle. `seasonMarketMAE` STAYS on the Records page too (its
   hindsight caveat still uses it). The Performance page is **deliberately UNLINKED** from every nav
   (reachable by direct URL only) — its top nav carries the other links but no self-link.

**`ui/build.py` now builds TWO pages** from `snapshots.json`: the full payload into
`projections.html` (Records, as before), and a **slim** payload (only per-team
`abbr`/`wins`/`actual`/`mkt_wins` + `projection_history` + `meta.seasons` — the heavy `players`/`grid`
blobs stripped) into `performance/performance.html`, so the Performance page stays light (~45 KB vs
~1.6 MB). `.vercelignore`'s `template.html`/`build.py` patterns already exclude the new template at
any depth; `ui/vercel.json` adds the `/performance` rewrite + cache header.

`ui/template.html` + `ui/build.py` -> `ui/projections.html` **and**
`ui/performance/performance.html` (self-contained, data inlined). Rebuild after regenerating
projections:

```
python scripts/project_current.py && python ui/build.py
```

**Conference toggle: All / East / West (shipped 2026-08-18, both NBA and NHL Records pages).** A
third control in the same `.controls` bar (`conf` group of pills) filters the standings table by
conference — display only, never touches the model. Conference membership is a plain client-side
lookup keyed by team abbreviation (`NBA_CONFERENCE` in `ui/template.html`, `NHL_CONFERENCE` in
`ui/nhl_records/template.html`) rather than a data-pipeline field, since franchise conference
membership is stable and this avoids threading a new column through `snapshots.json` / the NHL
records builder for a display-only filter. Combines with the existing `filter team…` box as an AND
(both must match); the `N of 30` / `N of 32` count reflects either filter being active. NBA reuses
the method-toggle's pill markup and `.active`-class pattern (`#conf-all/east/west`); NHL reuses the
sort-pills' `data-conf` attribute pattern (`.pill.on`) already established for `data-sort`. No
Python/build-script change — both pages already inline the abbreviation as `t.abbr` (NBA) / `t.team`
(NHL), which is all the lookup needs.

Design decisions: 30 team rows on a **shared win axis**, so range widths are directly
comparable across teams — that comparability is the whole point, since range width carries
information. Uncertainty is encoded twice, in bar width and in hue (teal = tight, amber =
high turnover). Monospace for all data, system sans for prose.

Roster edits recompute in the browser, and this is exact rather than approximate: the
aggregation is a minute-weighted mean, so it can be reproduced client-side. A precomputed
per-team grid of simulated win distributions across rating offsets is interpolated, which
keeps real schedule strength intact without running a simulation in the browser.

**Caveats are stated in the page itself, deliberately:** that the model does not beat the
market on aggregate accuracy (7.39 vs 6.88 MAE — the page reads ours from `CURRENT_MAE` in
`ui/template.html`, so bump that constant whenever the shipped MAE changes), that single-player what-ifs extrapolate the
calibration slope further than the backtest validated, and (upcoming season) that the market
ring is shown-only and never an input. Do not remove them.

**Deployment: Vercel, private repo, `ui/` only.** GitHub Pages free requires a public repo;
Vercel Hobby deploys the private repo and still serves a public URL. The site is configured
to serve **only `ui/`** (Vercel *Root Directory* = `ui`), so `data/`, `nbaproj/`, and
`scripts/` are never uploaded or reachable. `ui/vercel.json` serves `projections.html` at
`/`; `ui/.vercelignore` keeps `build.py`/`template.html` out. Full steps in
[ui/DEPLOY.md](ui/DEPLOY.md).

**Team detail is PUBLIC — no gate (2026-08-07).** Every team's expanded detail panel (per-player
Off/Def and ≈Wins, RAPM `D↑/D↓` flags + All-NBA/All-Def badges, the disagreement + conviction
block, the offseason-move trade-undo, and the live what-if editor) renders for **everyone, no
login**. `ui/build.py` inlines the **full** payload — including each team's `players` array and the
what-if `grid` — directly into the self-contained `projections.html`, the same way the standalone
player leaderboard (`ui/nba_players`) already ships its per-player numbers. The client's `unlocked`
flag is now simply *"does the loaded payload carry that detail"* (`loadSeason` sets it from the
presence of `DATA.grid` + `t.players`); `renderPanel`/`teamView` render the full panel whenever it
is true. `lockedPanel()` remains only as a defensive fallback for a payload that somehow lacks
detail (it should never fire in the shipped build).

**Implication (accepted, owner's call):** the per-player Off/Def/≈Wins that were behind the old
password gate are now in page source — the same numbers the leaderboard already exposes publicly,
so this is consistent, not a new disclosure. `sessionStorage`'s `wc_premium` cache path is gone.

*History / how to re-gate:* the detail was briefly split into a public payload + a password-gated
serverless payload (`ui/api/premium.js`, `PREMIUM_PASSWORD`, `x-unlock-password`); the "Unlock
details" button (`doUnlock`/`syncUnlockBtn`) was removed 2026-08-06 and the whole gate removed
2026-08-07 (`ui/api/premium.js` deleted; `ui/build.py` no longer splits the payload). To re-gate
later, restore the public/premium split + the serverless function from git history and re-add an
unlock button that `fetch`es `/api/premium`, sets `unlocked`, and re-renders. For real
subscriptions that would still be the seam: per-user auth (Clerk/Supabase) + Stripe (see the auth
roadmap item).

**✅ League-wide player-impact leaderboard (shipped 2026-08-04, `ui/nba_players/`).** A standalone,
self-contained companion view — one sortable / searchable / team- and position-filterable table of
**every rostered player's projected impact league-wide**, per season, mirroring the NHL impact viewer
(`ui/nhl/`). Columns: rank, Player (`pos · TEAM` subtitle, external ↗ link — see "External player
links" above), Impact, Off, Def, **≈ Wins**, MPG, Age; default sort Impact desc, centered mini-bar on
Impact, sign-colored Off/Def/Impact/≈Wins, light/dark toggle, glossary + the honest caveats (Impact is
a per-100 rate not a win count; defense is the weakest metric; these are projected talent inputs).
`scripts/build_nba_players_ui.py` flattens `snapshots.json`'s per-team `players` into one league-wide
list per season (auto-detects seasons; dedupes players who appear on two rosters in a walk-forward
snapshot, preferring the row with a listed position — verified value-lossless) and inlines it into
`ui/nba_players/template.html` → `ui/nba_players/players.html` (`__DATA__` placeholder,
`allow_nan=False`). Rebuild after a snapshot rebuild:

**≈ Wins column (added 2026-08-05).** A Python port (`win_parts`/`team_cap_factor` in
`scripts/build_nba_players_ui.py`) of the main team app's client-side `winParts`/`teamCapFactor`
(`ui/template.html`) — the WAR-like (wins-above-replacement) translation of a player's Impact,
decomposed into offensive and defensive wins by the same off/def slopes, RAPM-blended defensive
deviation, and 240-min/game roster-cap factor the team rating itself uses. Computed at **build time**
in Python (this standalone page has no team what-if editor to recompute client-side), one call per
player against his own team's roster context from `snapshots.json`. **Verified bit-for-bit against the
live JS**: the exact `winParts` function was run in Node against the same BOS 2026-27 roster and
matched the Python port to 4 decimal places for every player checked. Rendered as a bold total with a
small `o / d` split line beneath (`.wtot`/`.wsplit`), mirroring the main app's roster-panel styling.

```
python scripts/build_nba_players_ui.py
```

**Public decision (owner, 2026-08-04): this leaderboard is PUBLIC.** It exposes per-player
Off/Def/Impact numbers with data inlined and its own Vercel route `/players → /nba_players/players`
(`ui/vercel.json`), no serverless gating. (As of 2026-08-07 the **team detail panels are public
too** — see "Team detail is PUBLIC" above — so the same numbers now appear in both places, no
longer a leaderboard-only exposure.) `ui/.vercelignore`'s directory-agnostic
`template.html`/`build.py` patterns already keep the build inputs out of the deploy; `scripts/` is
outside the `ui` root dir so the build script is never uploaded.

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
