# NBA — data inventory, sources, and landmines

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

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
That unblocked the live 2025-26 RAPM input, and the turnover-weighted blend then shipped (see
the RAPM section above).
