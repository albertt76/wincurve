"""Pull functions for each raw dataset we need from stats.nba.com.

Availability windows below were verified empirically against the live API, not
taken from documentation. Requesting a season before a metric existed returns an
empty frame rather than an error, which is easy to mistake for a fetch bug --
hence the explicit constants.
"""

from __future__ import annotations

import logging

import pandas as pd
from nba_api.stats.endpoints import (
    leaguedashplayerstats,
    leaguedashptdefend,
    leaguedashteamstats,
    leaguegamelog,
    leaguehustlestatsplayer,
)

from .cache import cached_fetch, seasons

log = logging.getLogger(__name__)

TIMEOUT = 60

# --- Verified availability windows (season start year) -----------------------
FIRST_BOX_SEASON = 2005      # advanced box score: confirmed 458 players in 2005-06
FIRST_TRACKING_SEASON = 2013  # shot/defensive tracking: 2012-13 returns empty
FIRST_HUSTLE_SEASON = 2016    # hustle: 2015-16 is a partial rollout (147 rows)

LAST_COMPLETE_SEASON = 2024   # most recent season with a full set of results


def player_advanced(season: str) -> pd.DataFrame:
    """Per-player advanced box score: usage, reb rates, on-court ratings."""
    return cached_fetch(
        "player_advanced",
        leaguedashplayerstats.LeagueDashPlayerStats,
        {
            "season": season,
            "measure_type_detailed_defense": "Advanced",
            "per_mode_detailed": "PerGame",
            "timeout": TIMEOUT,
        },
    )


def player_base(season: str) -> pd.DataFrame:
    """Per-player base box score per-100-possessions.

    Per-100 rather than per-game so counting stats are directly comparable across
    the pace drift from ~90 to ~100 possessions/game over our window.
    """
    return cached_fetch(
        "player_base_per100",
        leaguedashplayerstats.LeagueDashPlayerStats,
        {
            "season": season,
            "measure_type_detailed_defense": "Base",
            "per_mode_detailed": "Per100Possessions",
            "timeout": TIMEOUT,
        },
    )


def team_advanced(season: str) -> pd.DataFrame:
    """Team-level advanced stats including W/L and off/def rating -- our target."""
    return cached_fetch(
        "team_advanced",
        leaguedashteamstats.LeagueDashTeamStats,
        {
            "season": season,
            "measure_type_detailed_defense": "Advanced",
            "timeout": TIMEOUT,
        },
    )


def game_log(season: str) -> pd.DataFrame:
    """Team-game rows for the regular season (2 per game): schedule + results.

    Feeds the Monte Carlo simulation (real schedule, home/away, rest days) and
    lets us reconstruct opening-night rosters for point-in-time correctness.
    """
    return cached_fetch(
        "team_game_log",
        leaguegamelog.LeagueGameLog,
        {
            "season": season,
            "season_type_all_star": "Regular Season",
            "player_or_team_abbreviation": "T",
            "timeout": TIMEOUT,
        },
    )


def rim_defense(season: str) -> pd.DataFrame:
    """Opponent FG% when this player defends shots inside 6ft -- rim deterrence.

    Empty before 2013-14.
    """
    return cached_fetch(
        "rim_defense",
        leaguedashptdefend.LeagueDashPtDefend,
        {
            "season": season,
            "defense_category": "Less Than 6Ft",
            "timeout": TIMEOUT,
        },
    )


def hustle(season: str) -> pd.DataFrame:
    """Screen assists, deflections, contested shots, loose balls.

    Empty before 2015-16; only fully populated from 2016-17.
    """
    return cached_fetch(
        "hustle",
        leaguehustlestatsplayer.LeagueHustleStatsPlayer,
        {"season": season, "timeout": TIMEOUT},
    )


# --- Bulk orchestration ------------------------------------------------------

def _pull_range(name: str, fn, first: int, last: int) -> pd.DataFrame:
    """Fetch one dataset across a season range and stack it with a SEASON column."""
    frames = []
    for season in seasons(first, last):
        df = fn(season)
        if df.empty:
            log.warning("%s: %s returned no rows", name, season)
            continue
        df = df.copy()
        df["SEASON"] = season
        df["SEASON_START"] = int(season[:4])
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    log.info("%s: %d rows across %d seasons", name, len(out), out["SEASON"].nunique())
    return out


def pull_all(last_season: int = LAST_COMPLETE_SEASON) -> dict[str, pd.DataFrame]:
    """Fetch every raw dataset across its own valid availability window."""
    return {
        "player_advanced": _pull_range(
            "player_advanced", player_advanced, FIRST_BOX_SEASON, last_season),
        "player_base": _pull_range(
            "player_base", player_base, FIRST_BOX_SEASON, last_season),
        "team_advanced": _pull_range(
            "team_advanced", team_advanced, FIRST_BOX_SEASON, last_season),
        "game_log": _pull_range(
            "game_log", game_log, FIRST_BOX_SEASON, last_season),
        "rim_defense": _pull_range(
            "rim_defense", rim_defense, FIRST_TRACKING_SEASON, last_season),
        "hustle": _pull_range(
            "hustle", hustle, FIRST_HUSTLE_SEASON, last_season),
    }
