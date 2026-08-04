"""NHL team-season target table + franchise spine.

The projection target is **points percentage** -- points / (2 * games played) -- not
raw points, because several seasons in the window are not 82 games (see
``SHORTENED_SEASONS``). Points are a presentation-layer conversion (pct * 164, a full
82-game season's maximum). Modeling a rate keeps shortened seasons comparable, exactly
as the NBA project models win percentage rather than wins.

Franchise join key is ``franchise_id`` (stable across relocations), with one manual
bridge: the 2024 Arizona -> Utah move, which the NHL records as a NEW franchise.
"""

from __future__ import annotations

import pandas as pd

from .ingest import PROC

FULL_SEASON_GAMES = 82
FULL_SEASON_POINTS = 2 * FULL_SEASON_GAMES  # 164 = winning all 82 in regulation

# Seasons not played over the full 82 games (start-year keyed), with the schedule
# length. point_pct normalizes these automatically; they are flagged so the market
# comparison and any raw-points reporting can treat them separately (as in the NBA
# project's 2011-12 / 2020-21 handling).
#   2012-13: 48 games (lockout)
#   2019-20: 68-71 games, varying BY TEAM (covid stoppage before the bubble)
#   2020-21: 56 games (covid)
SHORTENED_SEASONS = {2012: 48, 2019: 71, 2020: 56}

# franchise_id bridge for relocations the NHL does NOT record as the same franchise.
# Utah (40) is the continuation of the Arizona/Phoenix Coyotes (28) roster-wise, but
# the league filed it as a new franchise; bridge it so one-year carryover and prior-
# season links follow the team. (Atlanta<->Winnipeg=35 and Phoenix<->Arizona=28 are
# already one franchise_id and need no bridge.)
FRANCHISE_BRIDGE = {40: 28}


def target_table() -> pd.DataFrame:
    """One row per (team, season): the record we project, on a stable franchise key.

    Columns: season_start, fid (bridged franchise id), team_id, team (tricode),
    team_name, gp, wins, losses, otl, ties, points, point_pct, gf, ga.
    """
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ref = pd.read_parquet(PROC / "team_reference.parquet")

    df = ts.merge(ref[["team_id", "franchise_id", "tricode"]],
                  left_on="teamId", right_on="team_id", how="left")
    if df["franchise_id"].isna().any():
        missing = df.loc[df["franchise_id"].isna(), "teamFullName"].unique()
        raise RuntimeError(f"team_summary rows with no franchise match: {missing}")

    df["fid"] = df["franchise_id"].replace(FRANCHISE_BRIDGE).astype(int)
    # Recompute point_pct exactly rather than trust the API's rounded field.
    df["point_pct"] = df["points"] / (2 * df["gamesPlayed"])

    out = pd.DataFrame({
        "season_start": df["season_start"].astype(int),
        "season": df["season_start"].map(lambda y: f"{y}-{str(y + 1)[-2:]}"),
        "fid": df["fid"],
        "team_id": df["teamId"].astype(int),
        "team": df["tricode"],
        "team_name": df["teamFullName"],
        "gp": df["gamesPlayed"].astype(int),
        "wins": df["wins"].astype(int),
        "losses": df["losses"].astype(int),
        "otl": df["otLosses"].fillna(0).astype(int),
        "ties": df.get("ties", pd.Series(0, index=df.index)).fillna(0).astype(int),
        "points": df["points"].astype(int),
        "point_pct": df["point_pct"],
        "gf": df["goalsFor"].astype(int),
        "ga": df["goalsAgainst"].astype(int),
    })
    return out.sort_values(["season_start", "team"]).reset_index(drop=True)


def add_prior_season(df: pd.DataFrame) -> pd.DataFrame:
    """Attach each team-season's immediately-preceding season point_pct (same fid).

    Left as NaN when the franchise did not play the prior season -- i.e. its debut
    (Vegas 2017-18, Seattle 2021-22), which correctly has no carryover signal.
    """
    df = df.sort_values(["fid", "season_start"]).copy()
    df["prev_point_pct"] = df.groupby("fid")["point_pct"].shift(1)
    df["prev_season_start"] = df.groupby("fid")["season_start"].shift(1)
    # Only count it as a prior season if it was the immediately-preceding year.
    gap = df["season_start"] - df["prev_season_start"] != 1
    df.loc[gap, "prev_point_pct"] = pd.NA
    return df.drop(columns="prev_season_start")
