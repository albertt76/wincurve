"""Player-season and player-team-season tables built from game logs.

Two tables, because they answer different questions:

- ``player_team_seasons`` -- one row per (player, team, season). Minutes here sum
  correctly to the team's true budget, so this is what team aggregation and minute
  allocation must use (Stages 3-4).
- ``player_seasons`` -- one row per (player, season), pooled across any teams he
  played for. This is the unit for talent and aging (Stage 2), since a midseason
  trade does not make someone two different players.

Both derive from ``player_game_log`` rather than the season-level player table,
which misattributes traded players entirely to one team (see ingest.player_game_log).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .cache import DATA_DIR

PROC = DATA_DIR / "processed"

# Counting stats we aggregate. All are additive over games, which is what lets us
# sum them and only then convert to rates.
COUNTING = ["FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB", "REB",
            "AST", "STL", "BLK", "TOV", "PF", "PTS", "PLUS_MINUS"]

# Minimum minutes for a player-season to get a *rate* estimate. Below this, per-100
# rates are dominated by sampling noise -- a 40-minute season can show a 30% usage
# rate on three shots. Such players still appear in the minute accounting; they just
# don't get treated as having a measured skill level.
MIN_MINUTES_FOR_RATES = 250


def _load_game_logs() -> pd.DataFrame:
    df = pd.read_parquet(PROC / "player_game_log.parquet")
    keep = ["SEASON", "SEASON_START", "PLAYER_ID", "PLAYER_NAME", "TEAM_ID",
            "TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "MIN"] + COUNTING
    present = [c for c in keep if c in df.columns]
    missing = set(COUNTING) - set(df.columns)
    if missing:
        # PLUS_MINUS in particular is absent in some historical seasons.
        for col in missing:
            df[col] = np.nan
        present = keep
    return df[present]


def _pace_by_team_season() -> pd.DataFrame:
    """Team pace (possessions per 48 min), used to convert minutes to possessions."""
    t = pd.read_parquet(PROC / "team_advanced.parquet")
    return t[["TEAM_ID", "SEASON_START", "PACE"]].rename(
        columns={"TEAM_ID": "team_id", "SEASON_START": "season_start",
                 "PACE": "pace"})


def _debut_year() -> pd.DataFrame:
    """Map player -> NBA debut season start year, for experience."""
    bio = pd.read_parquet(PROC / "player_bio.parquet")
    out = pd.DataFrame({
        "player_id": bio["PERSON_ID"].astype("int64"),
        "debut_year": pd.to_numeric(bio["FROM_YEAR"], errors="coerce"),
    })
    return out.dropna(subset=["debut_year"]).drop_duplicates("player_id")


def _age_by_player_season() -> pd.DataFrame:
    """Age from the season-level advanced table (nba_api reports age within season)."""
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    return pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce"),
    }).drop_duplicates(["player_id", "season_start"])


def build_player_team_seasons() -> pd.DataFrame:
    """One row per (player, team, season) with minutes and summed box stats."""
    logs = _load_game_logs()
    grouped = logs.groupby(
        ["SEASON_START", "PLAYER_ID", "TEAM_ID"], as_index=False
    ).agg(
        season=("SEASON", "first"),
        player_name=("PLAYER_NAME", "first"),
        team=("TEAM_ABBREVIATION", "first"),
        games=("GAME_ID", "nunique"),
        minutes=("MIN", "sum"),
        **{c.lower(): (c, "sum") for c in COUNTING},
    )
    grouped = grouped.rename(columns={
        "SEASON_START": "season_start", "PLAYER_ID": "player_id",
        "TEAM_ID": "team_id"})
    return grouped.sort_values(["season_start", "team_id", "minutes"],
                               ascending=[True, True, False]).reset_index(drop=True)


def build_player_seasons() -> pd.DataFrame:
    """One row per (player, season), pooled across teams, with rates and context.

    Rates are per 100 possessions, using minute-weighted team pace so that the
    ~90 -> ~100 possessions/game drift across our window does not masquerade as
    players getting better.
    """
    pts = build_player_team_seasons()
    pace = _pace_by_team_season()

    # Possessions for each stint: minutes * (team possessions per minute).
    stint = pts.merge(pace, on=["team_id", "season_start"], how="left")
    stint["poss"] = stint["minutes"] * stint["pace"] / 48.0

    agg = {c.lower(): (c.lower(), "sum") for c in COUNTING}
    ps = stint.groupby(["season_start", "player_id"], as_index=False).agg(
        season=("season", "first"),
        player_name=("player_name", "first"),
        games=("games", "sum"),
        minutes=("minutes", "sum"),
        poss=("poss", "sum"),
        n_teams=("team_id", "nunique"),
        team=("team", "last"),          # team he finished the season with
        team_id=("team_id", "last"),
        **agg,
    )

    # Per-100-possession rates.
    for c in [c.lower() for c in COUNTING if c != "PLUS_MINUS"]:
        ps[f"{c}_p100"] = np.where(ps["poss"] > 0, ps[c] * 100.0 / ps["poss"], np.nan)

    # Efficiency: true shooting percentage (points per shooting possession, where a
    # trip to the line counts as ~0.44 of a possession).
    tsa = ps["fga"] + 0.44 * ps["fta"]
    ps["ts_pct"] = np.where(tsa > 0, ps["pts"] / (2 * tsa), np.nan)
    ps["fg3_rate"] = np.where(ps["fga"] > 0, ps["fg3a"] / ps["fga"], np.nan)
    ps["ft_rate"] = np.where(ps["fga"] > 0, ps["fta"] / ps["fga"], np.nan)

    # Context: age and experience.
    ps = ps.merge(_age_by_player_season(), on=["player_id", "season_start"], how="left")
    ps = ps.merge(_debut_year(), on="player_id", how="left")
    ps["experience"] = ps["season_start"] - ps["debut_year"]
    # A negative value means the directory disagrees with the game logs; trust the
    # logs and treat the player as a rookie rather than propagating a bad value.
    ps.loc[ps["experience"] < 0, "experience"] = 0

    ps["has_rates"] = ps["minutes"] >= MIN_MINUTES_FOR_RATES
    return ps.sort_values(["season_start", "minutes"],
                          ascending=[True, False]).reset_index(drop=True)


def zscore_within_season(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Standardise each column within season, minutes-weighted.

    Within-season standardisation is how we neutralise era drift: what matters is
    a player's standing relative to his own league-year, not a raw rate that shifts
    as the league changes. Weighting by minutes keeps deep-bench noise from
    distorting the reference distribution.
    """
    out = df.copy()
    for col in cols:
        z = np.full(len(out), np.nan)
        for _, idx in out.groupby("season_start").groups.items():
            sub = out.loc[idx]
            ok = sub[col].notna() & sub["has_rates"]
            if ok.sum() < 20:
                continue
            w = sub.loc[ok, "minutes"].to_numpy()
            v = sub.loc[ok, col].to_numpy()
            mean = np.average(v, weights=w)
            sd = np.sqrt(np.average((v - mean) ** 2, weights=w))
            if sd > 0:
                z[out.index.get_indexer(sub.loc[ok].index)] = (v - mean) / sd
        out[f"{col}_z"] = z
    return out
