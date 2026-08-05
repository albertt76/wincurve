"""Stage 3b: honest, point-in-time roster + time-on-ice reconstruction.

The Stage 4/5 team aggregation was a LEAKY upper bound: ``aggregate.player_toi(Y)`` reads season
``Y``'s ACTUAL MoneyPuck 5v5 rows, so it knows each team's full-season roster (including February
trade-deadline acquisitions) AND every skater's realized minutes -- both unknowable when a
pre-season projection is made. This module supplies the honest replacements, mirroring the NBA
project's ``roster_opening_day`` + prior-minutes approach:

1. ``opening_roster`` -- each team's roster **as the season starts**, reconstructed from the shift
   charts' first-appearance ordering (no source of true pre-season rosters exists this far back, so
   we reconstruct from the first games, exactly as the NBA project does). A skater is on team T's
   opening roster iff his **season debut was for T** and fell within **T's first ``k_games``**. That
   one rule handles trades correctly by construction: a player dealt to T at the deadline debuts for
   his OLD team, so he is excluded from T (and kept on the old team, which is right for opening day);
   a player dealt AWAY from T mid-season debuted for T and is kept. Validated on 2023-24: Jake
   Guentzel (PIT->CAR at the deadline) lands on PIT's opening roster, never CAR's.

2. ``projected_toi`` -- each skater's expected 5v5 minutes from his **prior** season (MoneyPuck
   ``Y-1``), not his realized season-``Y`` minutes. First-year / no-prior skaters get a modest
   bottom-rotation prior (``ROOKIE_TOI_SEC``).

``honest_toi`` joins the two into the ``(player_id, team, icetime)`` frame that
``aggregate.team_ratings`` consumes through its ``toi=`` hook, so the honest projection reuses the
exact same minute-weighted aggregation, replacement-level fill, and calibration as the leaky bound
-- only the roster set and the minute weights change.

**Point-in-time note (same peek the NBA project accepts):** reconstructing the opening roster reads
the first ``k_games`` of season ``Y``. The opening-night roster is essentially fixed before the
season, so this is a mild, standard peek -- it reveals *who is on the team*, never season-``Y``
usage or outcomes (minutes come strictly from ``Y-1``). Residual imperfections, stated not hidden:
a genuine opening-roster player who is injured/scratched through his team's first ``k_games`` is
missed (his minutes fill at replacement), and a waiver/callup who *debuts* for T inside the window
is wrongly kept -- both are low-minute and wash out under minute-weighting, as in the NBA analog.
"""

from __future__ import annotations

import pandas as pd

from . import ingest

# A skater is on the opening roster iff his season debut for the team is within the team's first
# this-many games. Tuned on 2023-24: coverage of a team's actual 5v5 minutes plateaus by ~16-20
# games at ~0.89 (matching the leaky bound's ~0.88), well before any trade deadline (~game 55-60),
# and roster sizes stay a sensible ~22-24 skaters. Not a knife-edge -- the debut rule does most of
# the trade-exclusion work; the window only bounds mid-season callups/debuts.
FIRST_GAMES_WINDOW = 20

# Prior-season 5v5 TOI (seconds) assigned to a roster skater with no prior season (rookies / new to
# the league). ~500 min is a bottom-rotation load, just under the ~600-650 min 25th percentile of
# rotation regulars (measured 2022-2024). Second-order: these skaters also get replacement-level
# IMPACT, so their exact weight barely moves the minute-weighted team mean.
ROOKIE_TOI_SEC = 30000


def _goalie_ids() -> set[int]:
    """MoneyPuck goalie ids -- excluded from skater rosters (as in ``rapm.build_stints``)."""
    return set(pd.read_parquet(ingest.PROC / "moneypuck_goalies.parquet")
               ["playerId"].dropna().astype(int))


def opening_roster(season_start: int, k_games: int = FIRST_GAMES_WINDOW) -> pd.DataFrame:
    """Reconstruct each team's opening-day skater roster for ``season_start`` from shift charts.

    Returns one row per ``(team, player_id)`` where ``team`` is the NHL tricode. A skater is kept
    iff his **first game of the season was for this team** (so mid-season arrivals, who debuted
    elsewhere, are excluded) AND that debut fell within the team's first ``k_games`` (so mid-season
    call-ups / debuts are excluded). See the module docstring for why this handles trades correctly.
    """
    sh = pd.read_parquet(ingest.PROC / f"shifts_{season_start}.parquet")[
        ["gameId", "playerId", "teamId"]].copy()
    # A few seasons (e.g. the covid-shortened 2019-20) carry a handful of malformed shift rows with
    # a missing playerId/teamId -- drop them before the int cast (they are unusable either way).
    sh = sh.dropna(subset=["playerId", "teamId"])
    sh["playerId"] = sh["playerId"].astype(int)
    sh = sh[~sh["playerId"].isin(_goalie_ids())]                      # skaters only

    # first game the skater appears for each team, and his first game for ANY team this season
    first_pt = sh.groupby(["playerId", "teamId"])["gameId"].min().reset_index(name="first_pt")
    first_any = sh.groupby("playerId")["gameId"].min()
    first_pt["first_any"] = first_pt["playerId"].map(first_any)

    # rank of each team's games by date (gameId is date-ordered within a season) -> the debut's
    # index in the team's schedule; keep debuts inside the opening window that were season debuts
    idx = (sh.groupby("teamId")["gameId"].rank(method="dense").astype(int) - 1)
    game_rank = dict(zip(zip(sh["teamId"], sh["gameId"]), idx))
    first_pt["team_game_idx"] = [game_rank[(t, g)]
                                 for t, g in zip(first_pt["teamId"], first_pt["first_pt"])]
    kept = first_pt[(first_pt["first_pt"] == first_pt["first_any"])   # season debut for THIS team
                    & (first_pt["team_game_idx"] < k_games)]          # inside the opening window

    ref = pd.read_parquet(ingest.PROC / "team_reference.parquet")[["team_id", "tricode"]]
    out = kept.merge(ref, left_on="teamId", right_on="team_id")
    return (out[["tricode", "playerId"]]
            .rename(columns={"tricode": "team", "playerId": "player_id"})
            .reset_index(drop=True))


def projected_toi(season_start: int, rookie_toi_sec: float = ROOKIE_TOI_SEC) -> pd.Series:
    """player_id -> projected 5v5 TOI (seconds) = his PRIOR season's total 5v5 icetime.

    A skater with no prior-season 5v5 row is not in the returned series; the caller fills him at
    ``rookie_toi_sec`` (kept as a parameter here for documentation/reuse).
    """
    sk = pd.read_parquet(ingest.PROC / "moneypuck_skaters.parquet")
    prev = sk[(sk["season_start"] == season_start - 1) & (sk["situation"] == "5on5")]
    toi = prev.groupby("playerId")["icetime"].sum()
    toi.index = toi.index.astype(int)
    return toi.rename("icetime")


def honest_toi(season_start: int, k_games: int = FIRST_GAMES_WINDOW,
               rookie_toi_sec: float = ROOKIE_TOI_SEC) -> pd.DataFrame:
    """The honest ``(player_id, team, icetime)`` frame -- a drop-in for ``aggregate.player_toi``.

    Opening-day roster (season ``Y``) weighted by each skater's PRIOR-season (``Y-1``) 5v5 TOI;
    roster skaters with no prior season get ``rookie_toi_sec``. Feed to
    ``aggregate.team_ratings(impacts, Y, toi=honest_toi(Y))``.
    """
    roster = opening_roster(season_start, k_games)
    prior = projected_toi(season_start, rookie_toi_sec)
    roster["player_id"] = roster["player_id"].astype(int)
    roster["icetime"] = roster["player_id"].map(prior).fillna(rookie_toi_sec)
    return roster[["player_id", "team", "icetime"]]


# --- LIVE upcoming-season roster (Stage 6) ---------------------------------
# The backtest reconstructs each opening roster from that season's shift charts. The UPCOMING season
# has none yet, so the live roster comes straight from the NHL web API (`ingest.roster`), the hockey
# analog of the NBA project's `commonteamroster` snapshot. A projection is only as current as its
# pull date -- trades continue all season -- so the caller records the snapshot date.
def active_tricodes(last_season: int = ingest.LAST_SEASON) -> list[str]:
    """The current NHL teams -- tricodes with a team-summary row in the last completed season."""
    ts = pd.read_parquet(ingest.PROC / "team_summary.parquet")
    ids = set(ts[ts["season_start"] == last_season]["teamId"])
    ref = pd.read_parquet(ingest.PROC / "team_reference.parquet")
    return sorted(ref[ref["team_id"].isin(ids)]["tricode"])


def live_roster(target_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """Current skater roster for every active team for season ``target_year`` from the NHL web API.

    Returns ``(team, player_id, pos)`` for forwards + defensemen (goalies excluded, as skater impact
    is 5v5). This is a live snapshot -- re-pull with ``refresh=True`` to pick up later moves.
    """
    frames = []
    for tri in active_tricodes():
        r = ingest.roster(tri, target_year, refresh=refresh)
        frames.append(r[r["group"] != "goalies"][["tricode", "player_id", "pos"]]
                      .rename(columns={"tricode": "team"}))
    return pd.concat(frames, ignore_index=True)


def live_toi(target_year: int, rookie_toi_sec: float = ROOKIE_TOI_SEC, *,
             refresh: bool = False) -> pd.DataFrame:
    """Live ``(player_id, team, icetime)`` for the UPCOMING season -- a drop-in for ``honest_toi``.

    The current API roster (``live_roster``) weighted by each skater's PRIOR-season 5v5 TOI
    (``projected_toi(target_year)`` = season ``target_year-1``); skaters with no prior season get
    ``rookie_toi_sec``. Feed to ``aggregate.team_ratings(project(target_year-1), target_year,
    toi=live_toi(target_year))``.
    """
    roster = live_roster(target_year, refresh=refresh).copy()
    prior = projected_toi(target_year, rookie_toi_sec)
    roster["player_id"] = roster["player_id"].astype(int)
    roster["icetime"] = roster["player_id"].map(prior).fillna(rookie_toi_sec)
    return roster[["player_id", "team", "icetime"]]
