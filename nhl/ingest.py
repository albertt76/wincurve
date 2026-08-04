"""NHL data ingest: point-in-time pulls, with availability windows as constants.

Sources (all free, no key):
- **NHL stats API** (``api.nhle.com/stats/rest/en``) -- team-season summaries
  (records, points, goals, PP%/PK%), shift charts (on-ice units for RAPM).
- **NHL web API** (``api-web.nhle.com/v1``) -- season index (rule flags per season),
  rosters, per-game play-by-play (events with a strength ``situationCode``).
- **MoneyPuck** (``moneypuck.com``) -- shot-level and season-summary expected goals
  (xG) at team / skater / goalie level, split by situation (even strength vs PP/PK).

Everything routes through ``core.httpcache.HttpCache`` (throttled, retrying, cached
to ``data/nhl/raw``), so a full historical pull is idempotent and re-runs are free.

Availability windows (empirically probed 2026-08; verify on pull, never assume):
- team summaries / standings: all seasons (the API serves back to 1917-18).
- MoneyPuck xG: **2007-08 onward** (its first season).
- shift charts (the ``/shiftcharts`` REST endpoint, on-ice units for RAPM): **2010-11
  onward**. Empirically probed 2026-08: 2007-08/2008-09/2009-10 return an EMPTY ``data``
  array for every game (openers AND mid-season), 2010-11 is fully populated. So the shift
  endpoint's floor is 3 seasons *later* than xG's, and RAPM cannot use 2007-2009.
So there are TWO backbones: the **xG / team-summary backbone is 2007-08 -> 2025-26**, but the
**shift / RAPM backbone is 2010-11 -> 2025-26** (``FIRST_SHIFT_SEASON``). Team-summary history
is pulled a bit wider (2005-06) only to give the walk-forward earlier training. (2007-2009 shift
data would need the NHL HTML shift reports, a messier source; deferred.)
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pandas as pd

from core.httpcache import HttpCache

log = logging.getLogger(__name__)

STATS_API = "https://api.nhle.com/stats/rest/en"
WEB_API = "https://api-web.nhle.com/v1"
MONEYPUCK = "https://moneypuck.com/moneypuck/playerData"
MP_DOWNLOADS = "https://peter-tanner.com/moneypuck/downloads"  # bulk shot files (zipped)

# Slim set of shot columns kept for xG-RAPM (the full file has 124). teamCode is the
# SHOOTING team; time is absolute game seconds; xGoal is the expected-goals value.
SHOT_COLS = ["game_id", "season", "isPlayoffGame", "period", "time", "event",
             "teamCode", "homeTeamCode", "awayTeamCode", "xGoal", "goal",
             "homeSkatersOnIce", "awaySkatersOnIce", "shotWasOnGoal",
             "goalieIdForShot", "shooterPlayerId"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "nhl"
PROC = DATA_DIR / "processed"

# Model backbones. Team-summary pull starts a bit earlier for training.
FIRST_SEASON = 2007          # 2007-08, the MoneyPuck xG floor
FIRST_SHIFT_SEASON = 2010    # 2010-11, the /shiftcharts floor (2007-2009 return empty) -> RAPM floor
LAST_SEASON = 2025           # 2025-26, last completed season
FIRST_SUMMARY_SEASON = 2005  # extra team-record history for the walk-forward
REGULAR = 2                  # gameTypeId: 2 = regular season, 3 = playoffs

HTTP = HttpCache(DATA_DIR / "raw", min_interval=0.3)


def season_id(start_year: int) -> int:
    """2023 -> 20232024. The NHL API's seasonId format."""
    return start_year * 10000 + (start_year + 1)


def season_str(start_year: int) -> str:
    """2023 -> '2023-24'."""
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def seasons(first: int = FIRST_SEASON, last: int = LAST_SEASON) -> list[int]:
    return list(range(first, last + 1))


# --- reference / franchise spine -------------------------------------------
def team_reference(*, refresh: bool = False) -> pd.DataFrame:
    """All franchises ever, with the stable ``franchiseId`` join key.

    ``franchiseId`` bridges relocations (Atlanta Thrashers <-> Winnipeg Jets = 35;
    Phoenix <-> Arizona Coyotes = 28) the way NBA ``TEAM_ID`` does -- EXCEPT the
    2024 Arizona->Utah move, which the NHL records as a *new* franchise (id 40, vs
    Arizona's 28). That one discontinuity is bridged manually in ``nhl/teams.py``.
    """
    data = HTTP.get_json(f"{STATS_API}/team", refresh=refresh)["data"]
    df = pd.DataFrame(data)
    return df[["id", "franchiseId", "fullName", "triCode"]].rename(
        columns={"id": "team_id", "franchiseId": "franchise_id",
                 "fullName": "team_name", "triCode": "tricode"})


def season_index(*, refresh: bool = False) -> pd.DataFrame:
    """Per-season rule flags -- the point-in-time correctness backbone.

    Tells us, for each season, whether the loser point was in use
    (``pointForOTlossInUse``), whether ties existed (``tiesInUse``; pre-2005-06),
    and the exact ``standingsStart``/``standingsEnd`` dates. This is how we know
    which scoring rules applied without hard-coding eras.
    """
    data = HTTP.get_json(f"{WEB_API}/standings-season", refresh=refresh)["seasons"]
    df = pd.DataFrame(data)
    df["season_start"] = df["id"] // 10000
    return df


# --- team-season records (the target table foundation) ---------------------
def team_summary(start_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """Regular-season team records for one season: points, W/L/OTL, goals, PP/PK."""
    sid = season_id(start_year)
    payload = HTTP.get_json(
        f"{STATS_API}/team/summary",
        params={"cayenneExp": f"seasonId={sid} and gameTypeId={REGULAR}",
                "limit": 100, "start": 0},
        refresh=refresh,
    )
    total, data = payload.get("total", 0), payload["data"]
    # Fail loud if the API ever pages (would silently drop teams); 32 teams < 100.
    if total != len(data):
        raise RuntimeError(f"team_summary {sid}: got {len(data)} of {total} rows (paging?)")
    return pd.DataFrame(data).assign(season_start=start_year)


# --- MoneyPuck expected-goals (impact-layer inputs; pulled now for inventory) ---
def moneypuck(kind: str, start_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """MoneyPuck season summary CSV. ``kind`` in {teams, skaters, goalies}.

    Rows are split by ``situation`` (all / 5on5 / 5on4 / 4on5 / other), which is what
    lets even-strength impact and special teams be separated downstream.
    """
    text = HTTP.get_text(f"{MONEYPUCK}/seasonSummary/{start_year}/regular/{kind}.csv",
                         refresh=refresh)
    # MoneyPuck CSVs have 100+ columns and read in as a fragmented frame; copy() to
    # consolidate blocks before adding a column (avoids a pandas PerformanceWarning).
    df = pd.read_csv(io.StringIO(text)).copy()
    df["season_start"] = start_year
    return df


# --- roster / play-by-play / shifts (structure verified now; full pull deferred) ---
def roster(tricode: str, start_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """Team roster for a season (forwards/defensemen/goalies)."""
    sid = season_id(start_year)
    payload = HTTP.get_json(f"{WEB_API}/roster/{tricode}/{sid}", refresh=refresh)
    rows = []
    for group in ("forwards", "defensemen", "goalies"):
        for p in payload.get(group, []):
            rows.append({
                "player_id": p["id"], "group": group,
                "first": p["firstName"].get("default"),
                "last": p["lastName"].get("default"),
                "pos": p.get("positionCode"),
                "tricode": tricode, "season_start": start_year,
            })
    return pd.DataFrame(rows)


def game_pbp(game_id: int, *, refresh: bool = False) -> dict:
    """Raw play-by-play JSON for one game (events + strength situationCode)."""
    return HTTP.get_json(f"{WEB_API}/gamecenter/{game_id}/play-by-play", refresh=refresh)


def player_landing(player_id: int, *, refresh: bool = False) -> dict:
    """NHL player 'landing' bio record -- carries ``birthDate`` (for aging), position, etc."""
    return HTTP.get_json(f"{WEB_API}/player/{player_id}/landing", refresh=refresh)


def shifts(game_id: int, *, refresh: bool = False) -> pd.DataFrame:
    """Shift chart for one game: who was on the ice when (on-ice units for RAPM)."""
    data = HTTP.get_json(f"{STATS_API}/shiftcharts",
                         params={"cayenneExp": f"gameId={game_id}"}, refresh=refresh)["data"]
    return pd.DataFrame(data)


# --- bulk xG-RAPM inputs: MoneyPuck shots (the response) + shift charts (on-ice units) ---
def full_game_id(start_year: int, mp_game_id: int) -> int:
    """MoneyPuck game_id (last 5 digits) -> full NHL gameId. 2023,20001 -> 2023020001."""
    return start_year * 1_000_000 + int(mp_game_id)


def moneypuck_shots(start_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """All unblocked shot attempts for a season, with xG -- the RAPM response.

    Downloads MoneyPuck's zipped season shot file (cached as bytes), returns a slim
    frame with the full NHL ``gid`` attached. ``time`` is absolute game seconds;
    ``teamCode`` is the shooting team; ``xGoal`` is the expected-goals value.
    """
    raw = HTTP.get_bytes(f"{MP_DOWNLOADS}/shots_{start_year}.zip", refresh=refresh)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, usecols=lambda c: c in SHOT_COLS)
    df["gid"] = full_game_id(start_year, 0) + df["game_id"].astype(int)
    return df


def regular_game_ids(start_year: int, *, refresh: bool = False) -> list[int]:
    """Full NHL gameIds for a season's regular-season games (from the shot file)."""
    sh = moneypuck_shots(start_year, refresh=refresh)
    reg = sh.loc[sh["isPlayoffGame"] == 0, "gid"].unique()
    return sorted(int(g) for g in reg)


def pull_season_shifts(start_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """Pull + cache every regular-season game's shift chart for a season.

    The heavy RAPM input: ~1300 games/season, one cached call each. Idempotent and
    resumable. Writes ``data/nhl/processed/shifts_<start_year>.parquet``.
    """
    PROC.mkdir(parents=True, exist_ok=True)
    out = PROC / f"shifts_{start_year}.parquet"
    if out.exists() and not refresh:
        return pd.read_parquet(out)

    gids = regular_game_ids(start_year, refresh=refresh)
    keep = ["gameId", "playerId", "teamId", "period", "startTime", "endTime", "duration"]
    frames = []
    ok = empty = errored = 0
    for i, gid in enumerate(gids):
        try:
            s = shifts(gid, refresh=refresh)
        except Exception as err:  # noqa: BLE001 -- a fetch failure, distinct from an empty game
            log.warning("shifts %d unavailable: %s", gid, err)
            errored += 1
            continue
        # An empty response (whole seasons < FIRST_SHIFT_SEASON, or the rare single game) is a
        # legitimate skip; a raised exception above is a fetch failure. Track them separately.
        if not s.empty and "gameId" in s.columns:
            frames.append(s[[c for c in keep if c in s.columns]])
            ok += 1
        else:
            empty += 1
        if (i + 1) % 200 == 0:
            log.info("  shifts %d/%d games", i + 1, len(gids))

    if not frames:
        # No usable data at all. State the cause: mostly-errored = a fetch failure to retry;
        # mostly-empty = a genuinely pre-floor season (the /shiftcharts endpoint is empty before
        # FIRST_SHIFT_SEASON = 2010-11). Do not write a parquet either way.
        raise RuntimeError(
            f"no shift-chart data for {season_str(start_year)}: {empty} empty, {errored} errored "
            f"of {len(gids)} games. Mostly errored -> fetch failure, retry. Mostly empty -> the "
            f"/shiftcharts endpoint serves {season_str(FIRST_SHIFT_SEASON)} onward (earlier: none).")
    if errored:
        # Some games failed to fetch -- refuse to cache a silently-incomplete season (cf.
        # team_summary's paging assert). Successful games are HTTP-cached, so a re-run retries
        # only the failures and is cheap/resumable; the season writes only once it is complete.
        raise RuntimeError(
            f"{season_str(start_year)}: {errored} of {len(gids)} games failed to fetch "
            f"({ok} ok, {empty} empty) -- not caching a partial season; re-run to retry "
            f"(cached games return instantly).")
    df = pd.concat(frames, ignore_index=True)
    df.to_parquet(out, index=False)
    log.info("shifts %s: %d games (%d empty), %d shift rows -> %s",
             season_str(start_year), ok, empty, len(df), out.name)
    return df


# --- Stage 0 orchestration --------------------------------------------------
def pull_all(*, refresh: bool = False) -> dict:
    """Pull the Stage 0 foundational datasets and write them to ``data/nhl/processed``.

    Idempotent: cached calls make re-runs free. Play-by-play + shift charts (the
    thousands-of-games RAPM pull) are deliberately NOT pulled here -- their structure
    is verified separately and the bulk pull belongs to the impact stage.
    """
    PROC.mkdir(parents=True, exist_ok=True)
    written = {}

    ref = team_reference(refresh=refresh)
    ref.to_parquet(PROC / "team_reference.parquet", index=False)
    written["team_reference"] = len(ref)

    idx = season_index(refresh=refresh)
    idx.to_parquet(PROC / "season_index.parquet", index=False)
    written["season_index"] = len(idx)

    summaries = pd.concat(
        [team_summary(y, refresh=refresh)
         for y in range(FIRST_SUMMARY_SEASON, LAST_SEASON + 1)],
        ignore_index=True)
    summaries.to_parquet(PROC / "team_summary.parquet", index=False)
    written["team_summary"] = len(summaries)

    for kind in ("teams", "skaters", "goalies"):
        frames = []
        for y in range(FIRST_SEASON, LAST_SEASON + 1):
            try:
                frames.append(moneypuck(kind, y, refresh=refresh))
            except Exception as err:  # noqa: BLE001
                log.warning("moneypuck %s %d unavailable: %s", kind, y, err)
        if frames:
            out = pd.concat(frames, ignore_index=True)
            out.to_parquet(PROC / f"moneypuck_{kind}.parquet", index=False)
            written[f"moneypuck_{kind}"] = len(out)

    return written
