# NBA — offseason movement, known absences, and injury returns

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

### Offseason movement & absences — how each is handled

| Factor | Status | Mechanism |
|---|---|---|
| Trades, free agency | ✅ | `commonteamroster` live snapshot (2026-27 already posted) |
| Retirements | ✅ | Retired players are simply absent from rosters |
| First-round picks / rookies | ✅ | Draft-position priors in `nbaproj/rosters.py` |
| Injury/suspension **risk** | ✅ | Availability model from games-missed history + age |
| **Specific known absences** | ⚠️ manual | `data/overrides/known_absences.json` — no free feed exists |
| **Returning-from-injury stars** | ⚠️ manual | `data/overrides/injury_returns.json` — the inverse override (see below) |

**A projection is only as current as its snapshot date.** Trades continue all season; a
July projection cannot know about a November deal. Always record the snapshot date with
any output. Historical injury *reasons* are unavailable — Pro Sports Transactions is
behind a Cloudflare bot challenge we will not bypass — so absences mix injury with
rest, suspension and coach's decision. Hence the name "availability", not "health".

### ✅ SHIPPED: injury-return override (the inverse of known absences)

A star who missed most or all of *last* season is mis-projected three ways, all fixed by
`data/overrides/injury_returns.json` + `nbaproj/rosters.py` (`load_return_overrides`,
`resolve_return_overrides`, `injured_season_mask`, `apply_return_overrides`), wired into
`scripts/project_current.py`:

1. **Minutes** fall back to the 8.0-mpg bench default, because `prior_mpg` reads only
   `LAST_HISTORY` and he supplied ~none — Haliburton was projected at **8.0 mpg**, an
   All-Star point guard as a deep reserve. This was the catastrophic bug.
2. **Availability** is dragged down by the games-missed model (Tatum **54%**).
3. **Talent** can be distorted by a partial injured-season sample (Tatum's 16-game 2025-26
   inflated his defense to +2.09 / deflated offense to +1.40 vs a healthy +1.7 / +1.8).

Each entry re-projects the player **as his last healthy season** (`basis_season`): his
post-injury partial seasons are dropped from the projection *inputs* (`injured_season_mask`
over both the box `imp` and the RAPM frame), so the normal aging + shrinkage restore his
pre-injury off/def — box **and** RAPM — with no hand-entered impact numbers; `prior_mpg`
becomes the basis season's minutes × an optional eased-in `minute_restriction`; and
`proj_availability` is **set** to `expected_availability` (a RAISE — unlike the absence
override, which floors via `min`). The input-filtering only fires for a basis within two
seasons of TARGET (else the reprojected player fails `project_next_season`'s "seen
recently" gate); minutes/availability restore for any basis.

**Live bundle only.** Like `known_absences.json`, it is manual, forward-looking, and applied
in `project_current.py` after the model runs — it **never touches the walk-forward
backtest** (which uses real historical rosters and cannot see the future), so there is no
gate to clear; it is a judgment overlay, and the numbers are user-editable. Per-player it
emits `ret_override` / `ret_reason`; the UI shows a purple **`back`** badge (hover = reason)
and a glossary entry.

Shipped list (2026-08-02) and effect. First five are confirmed Achilles/ACL returns
(~13–19 months out by opening night); the last four were added from the candidate scan on
user selection (availability is user judgment, Porziņģis discounted for durability):

| Player | Team | Before → after wins | Driver |
|---|---|---|---|
| Jayson Tatum | BOS | 59.3 → **62.3** | avail 54→97%, mpg 32.6→36.4, split cleaned |
| Tyrese Haliburton | IND | 32.9 → **34.7** | mpg **8.0→31.9** (the fallback fix) |
| Kyrie Irving | DAL | 34.0 → **35.9** | role + availability restored (eased) |
| Damian Lillard | POR | 40.4 → **42.1** | restored but eased (36 y/o post-Achilles) |
| Fred VanVleet | HOU | 48.8 → **48.0** | see below — went *down* |
| Walker Kessler | LAL | 46.2 → **48.8** | played 5 of 82; young, near-full return |
| Jalen Williams | OKC | 58.4 → **59.6** | missed ~half; young starter restored |
| Kristaps Porziņģis | GSW | 41.8 → **43.3** | restored but eased (72%, chronic durability) |
| Domantas Sabonis | SAC | 26.1 → **27.6** | missed most of last year, restored |

**Second review (2026-09-28), from the owner-approved shortlist** (see the shortlist entry in `docs/nba/shipped.md` for how each
player's effect was priced). Two settings: *clean* = 85% availability, full basis minutes;
*eased* = 70% availability, 90% minutes. Nine added, one revised; team numbers are the full
pipeline, before → after:

| Player | Team | Setting | Before → after wins | Note |
|---|---|---|---|---|
| Giannis Antetokounmpo | MIA | clean | 39.4 → **42.0** | healthy at media day |
| Anthony Davis | WAS | eased | 25.0 → **26.0** | age 34, chronic-absence history |
| Ja Morant | POR | eased | 42.5 → **43.2** | mixed health reports |
| Christian Braun | DEN | clean | 46.6 → **47.4** | durable before last season |
| Darius Garland | LAC | clean | 37.8 → **38.6** | |
| Tyler Herro | MIL | clean | 32.2 → **32.9** | |
| Zach LaVine | SAC | eased | 25.3 → **25.8** | |
| Joel Embiid | PHI | eased | 40.2 → **40.2** | rating +0.15, offset by the league-wide shift below |
| Stephen Curry | GSW | eased | 42.6 → **41.9** | combined with the Porziņģis revision |
| Kristaps Porziņģis (revised) | GSW | 72% → 50% | (in GSW above) | out indefinitely to open camp |

Because league wins are zero-sum (~1230), restoring nine players shifts every untouched team
down ~0.3 wins — the same partial-vs-full-equilibrium gap the what-if editor ignores. De'Aaron
Fox (SAS, ~7 games) was added to `known_absences.json` as a no-op under the floor rule; Jimmy
Butler was already there (44 games), which is what produces his 46% availability.

**The VanVleet lesson.** Our metric rated his last healthy season (2024-25) at **−1.1
impact** (below replacement), so "restore pre-injury level" restores a slightly *negative*
rating and Houston drops 0.8 — the real effect is fixing his minutes (bench fallback → ~33
mpg), not a boost. A legitimate, explainable disagreement (the user sees him as impactful;
the metric does not), surfaced rather than papered over. This is why the override restores
what the *metric* thought, not a reputation.

`scripts/injury_return_candidates.py` is the reusable, data-only scan that surfaces the
candidate pool (rostered, missed >half of last season, positive impact in a recent healthy
one) — a shortlist to review, **not** a list to import: whether each is a genuine return at
prior level (vs chronic absence, trade, rest, or age decline) and his prognosis are manual
calls. As of the 2026-09-28 review, every candidate worth ≥1 win to his team has been decided
(see the second-review table above); the remaining 17 move their team by under 1 win and are
left to the statistical availability model.

#### ⚠️ Injury-recovery offensive discount gated and REJECTED (2026-08-06) — real per-player, dies at team-win level

The review's third recommendation: `data/overrides/injury_returns.json` restores a returning star
straight to full pre-injury form, with no discount for the well-documented first-season-back dip.
**Pre-check** (`scripts/precheck_injury_recovery.py`, purely descriptive, no gate): a natural-
experiment scan of `player_impact.parquet` (games < 50% of that season's team-game count, a
healthy ≥`MIN_HEALTHY_MINUTES` season on record, and a next season observed) found a real signal
once properly isolated. The raw/unrestricted cohort (n=304) was contaminated by chronic-decline
and journeyman cases (a stale multi-year-old "healthy" basis, not a clean single-injury return) —
restricting to the classic immediate-return case (`seasons_since_basis == 2`, n=155) removed that
confound: **delta_impact −0.32 ±0.12 SE, offense-specific (delta_off −0.36 ±0.09 SE, ~4 SE from
zero; defense +0.04 ±0.07, no drag)**. A follow-up regression, though, found neither share-of-
season-missed nor age explains a significant SLOPE within that clean cohort (both |t| < 1.5,
r² ≈ 0.01) — consistent with this project's repeated finding that a second free parameter is
unidentifiable at this sample size (see the carryover-turnover rejection). So the model shipped to
gate was deliberately the simplest one the data supports: a **flat** `RECOVERY_OFFENSE_DISCOUNT =
-0.36` points/100, applied only to `proj_off_impact` for the immediate return season
(`target_season == basis_season + 2`), as a **default** for `injury_returns.json` entries (never
replacing the file's manual, forward-looking judgment) — `nbaproj.rosters.apply_recovery_discount`.

**Gate (`scripts/gate_injury_recovery.py`, 5000 sims, walk-forward 2017-2025, restricted to the
~76 team-seasons actually containing a qualifying returning player, since the discount cannot move
MAE for a team with none): decisively not helpful — delta −0.018 ±0.026 SE on affected teams
(−0.024 ±0.016 SE league-wide), only 1/6 folds improved.** Same shape as this project's other
"real at the player level, dies at the team-win number" rejections (the RAPM defensive swap, the
shrinkage-constant refit, the player-level RAPM blend family): a small, real per-player effect
spread over too few team-seasons (~8-17/season) to move an aggregate built from 30 teams'
worth of noise. Code kept (`apply_recovery_discount`, `RECOVERY_OFFENSE_DISCOUNT`,
`scripts/gate_injury_recovery.py`) but **not wired into the live pipeline** — the manual
`injury_returns.json` override stays exactly as it was (restores to basis level, no discount).
