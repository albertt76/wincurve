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
    commonallplayers,
    leaguedashplayerstats,
    leaguedashptdefend,
    leaguedashptstats,
    leaguedashteamstats,
    leaguegamelog,
    leaguehustlestatsplayer,
    playerawards,
)

from .cache import cached_fetch, season_str, seasons

log = logging.getLogger(__name__)

TIMEOUT = 60

# --- Verified availability windows (season start year) -----------------------
FIRST_BOX_SEASON = 2005      # advanced box score: confirmed 458 players in 2005-06
FIRST_TRACKING_SEASON = 2013  # shot/defensive tracking: 2012-13 returns empty
FIRST_HUSTLE_SEASON = 2016    # hustle: 2015-16 is a partial rollout (147 rows)

LAST_COMPLETE_SEASON = 2025   # 2025-26 verified complete (all 30 teams at 82 GP)


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


def player_game_log(season: str) -> pd.DataFrame:
    """Every player-game row for the regular season.

    This is the backbone of the whole system, and the season-level player table
    cannot replace it. ``LeagueDashPlayerStats`` returns exactly one row per player
    per season attributed to a *single* team, so a traded player's full-season
    production lands entirely on one of his teams. Team minute totals under that
    table range 17,248-21,960 against a true 19,680 (240 min x 82 games); from game
    logs they come in at 19,696-19,945, with the small excess correctly explained
    by overtime.

    Also supplies, for later stages:
      - games missed / availability (Stage 3)
      - opening-night roster reconstruction for point-in-time correctness
    """
    return cached_fetch(
        "player_game_log",
        leaguegamelog.LeagueGameLog,
        {
            "season": season,
            "season_type_all_star": "Regular Season",
            "player_or_team_abbreviation": "P",
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


# LeagueDashPtDefend's other nearest-defender categories -- the rim pull above ("Less Than 6Ft")
# is only one of six. Perimeter containment (the model's remaining named defensive weakness) has
# no countable events at the rim; these are the categories that could surface it. Same endpoint,
# same ToS surface, same 2013-14+ availability -- just a different `defense_category` parameter,
# which the cache key already includes, so each pulls and caches independently of the rim call.
SHOT_DEFENSE_CATEGORIES = ["Overall", "3 Pointers", "2 Pointers", "Less Than 10Ft",
                          "Greater Than 15Ft"]


def shot_defense(season: str, category: str) -> pd.DataFrame:
    """Opponent FG% when this player is the nearest defender, for one shot-distance category.

    `category` must be one of `SHOT_DEFENSE_CATEGORIES` (or "Less Than 6Ft", the rim pull already
    covered by `rim_defense`). Empty before 2013-14, same as `rim_defense`.
    """
    return cached_fetch(
        "shot_defense",
        leaguedashptdefend.LeagueDashPtDefend,
        {
            "season": season,
            "defense_category": category,
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


def player_passing(season: str) -> pd.DataFrame:
    """Passing-tracking creation stats: potential assists, points created, secondary
    ("hockey") assists, passes made -- the playmaking a raw assist count under-credits.

    Empty before 2013-14 (SportVU tracking era). PerGame; normalised by minutes downstream.
    """
    return cached_fetch(
        "player_passing",
        leaguedashptstats.LeagueDashPtStats,
        {
            "season": season,
            "season_type_all_star": "Regular Season",
            "player_or_team": "Player",
            "pt_measure_type": "Passing",
            "per_mode_simple": "PerGame",
            "timeout": TIMEOUT,
        },
    )


def player_awards(player_id: int) -> pd.DataFrame:
    """All honors for one player (All-NBA / All-Defensive team with 1/2/3 number, MVP,
    All-Star, etc.), keyed by SEASON. One call per player; cached per player_id.

    Used as an eye-test correction layer: prior-year All-Defensive selections flag the
    perimeter stoppers the box + tracking metric still under-credits (containment produces
    few countable events). Strictly a PRIOR-year signal, never contemporaneous, to avoid
    leakage.
    """
    return cached_fetch(
        "player_awards",
        playerawards.PlayerAwards,
        {"player_id": int(player_id), "timeout": TIMEOUT},
    )


def player_bio() -> pd.DataFrame:
    """League-wide player directory with debut year (``FROM_YEAR``).

    Needed because experience is left-censored in our window: a player whose first
    observed season is 2005-06 may well have debuted in 1998. Deriving experience
    from our own data alone would understate it for every pre-2005 holdover and
    silently corrupt the aging model. One call covers all players.
    """
    return cached_fetch(
        "player_bio",
        commonallplayers.CommonAllPlayers,
        {"is_only_current_season": 0, "league_id": "00", "timeout": TIMEOUT},
    )


def shot_defense_all_categories(season: str) -> pd.DataFrame:
    """All `SHOT_DEFENSE_CATEGORIES` for one season, stacked with a DEFENSE_CATEGORY column."""
    frames = []
    for cat in SHOT_DEFENSE_CATEGORIES:
        df = shot_defense(season, cat)
        if df.empty:
            continue
        df = df.copy()
        df["DEFENSE_CATEGORY"] = cat
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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
        "player_game_log": _pull_range(
            "player_game_log", player_game_log, FIRST_BOX_SEASON, last_season),
        "rim_defense": _pull_range(
            "rim_defense", rim_defense, FIRST_TRACKING_SEASON, last_season),
        "shot_defense": _pull_range(
            "shot_defense", shot_defense_all_categories, FIRST_TRACKING_SEASON, last_season),
        "hustle": _pull_range(
            "hustle", hustle, FIRST_HUSTLE_SEASON, last_season),
        "player_passing": _pull_range(
            "player_passing", player_passing, FIRST_TRACKING_SEASON, last_season),
        # Season-invariant: one row per player ever, no season loop.
        "player_bio": player_bio(),
    }


def team_roster(team_id: int, season: str) -> pd.DataFrame:
    """One team's season roster, including players who never played.

    Verified behaviour: this returns the **end-of-season** roster, not opening day.
    Blake Griffin, traded from the Clippers to the Pistons in January 2018, appears on
    Detroit's 2017-18 roster and not on the Clippers'. So it cannot be used raw for a
    point-in-time backtest.

    Its value is that it lists players who logged no minutes -- notably a star injured
    on opening night, whom a game-log reconstruction misses entirely even though such a
    player is well known before the season. Combined with game logs to strip midseason
    arrivals (see project.roster_opening_day), it gives a far better opening-day roster
    than either source alone.

    ``HOW_ACQUIRED`` is populated only for draft picks and carries no trade dates, so it
    cannot be used to identify midseason acquisitions.
    """
    from nba_api.stats.endpoints import commonteamroster
    return cached_fetch(
        "team_roster",
        commonteamroster.CommonTeamRoster,
        {"team_id": team_id, "season": season, "timeout": TIMEOUT},
    )


def pull_rosters(first: int, last: int) -> pd.DataFrame:
    """Rosters for every team across a season range (30 calls per season)."""
    from nba_api.stats.static import teams as static_teams
    team_ids = [t["id"] for t in static_teams.get_teams()]
    frames = []
    for start in range(first, last + 1):
        season = season_str(start)
        for tid in team_ids:
            df = team_roster(tid, season)
            if df.empty:
                continue
            df = df.copy()
            df["SEASON"] = season
            df["SEASON_START"] = start
            frames.append(df)
        log.info("rosters: finished %s", season)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def team_coaches(team_id: int, season: str) -> pd.DataFrame:
    """Coaching staff for one team-season (frame 1 of the roster endpoint).

    Note ``IS_ASSISTANT`` is NOT a boolean -- it is a rank code (1 = head coach,
    9 = associate head coach, 2 = assistant, and so on). Filter on
    ``COACH_TYPE == 'Head Coach'`` instead.

    ``SEASON`` here is the season *start* year as a string, unlike the roster frame.
    """
    from nba_api.stats.endpoints import commonteamroster
    return cached_fetch(
        "team_coaches",
        commonteamroster.CommonTeamRoster,
        {"team_id": team_id, "season": season, "timeout": TIMEOUT},
        frame=1,
    )


def pull_coaches(first: int, last: int) -> pd.DataFrame:
    """Head-coach assignments across a season range (30 calls per season)."""
    from nba_api.stats.static import teams as static_teams
    team_ids = [t["id"] for t in static_teams.get_teams()]
    frames = []
    for start in range(first, last + 1):
        season = season_str(start)
        for tid in team_ids:
            df = team_coaches(tid, season)
            if df.empty:
                continue
            df = df.copy()
            df["SEASON_START"] = start
            frames.append(df)
        log.info("coaches: finished %s", season)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
