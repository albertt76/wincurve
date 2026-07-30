"""Team-season table: the prediction target and the franchise spine.

Two things here are easy to get wrong and would silently corrupt every downstream
number:

1. **Shortened seasons.** Three seasons in the 2005-06..2024-25 window are not 82
   games (2011-12 lockout, 2019-20 COVID, 2020-21). In 2019-20 the game count
   varies *by team* (the bubble invited only 22 of 30). So the modeling target is
   win percentage; wins are a presentation-layer conversion.

2. **Franchise identity.** Teams relocate and rename (Seattle -> OKC, New Jersey ->
   Brooklyn, Charlotte Bobcats -> Hornets). ``TEAM_ID`` is stable per franchise
   across all of it, so it -- never the abbreviation or name -- is the join key.
"""

from __future__ import annotations

import pandas as pd

from .cache import DATA_DIR

FULL_SEASON_GAMES = 82

# Seasons with a non-standard schedule length. 2019-20 is deliberately absent:
# its game count varies by team, so it is derived per row rather than declared.
SHORTENED_SEASONS = {
    "2011-12": "lockout",
    "2012-13": "one BOS/IND game cancelled (Boston Marathon bombing)",
    "2019-20": "covid suspension + bubble",
    "2020-21": "covid shortened",
}

# 2019-20 is the one season worth considering for exclusion rather than rescaling:
# only 22 of 30 teams were invited to the bubble, so the eight excluded teams have
# a record from a truncated, differently-motivated sample. Flagged, not dropped --
# revisit if it shows up as an outlier fold in the walk-forward backtest.
SUSPECT_SEASONS = {"2019-20"}


def load_team_seasons() -> pd.DataFrame:
    """Team-season table with win_pct, games played, and ratings.

    One row per (franchise, season). ``wins_82`` is win_pct rescaled to a full
    season so shortened years are comparable; ``wins_actual`` is what really
    happened.
    """
    df = pd.read_parquet(DATA_DIR / "processed" / "team_advanced.parquet")

    out = pd.DataFrame({
        "team_id": df["TEAM_ID"].astype("int64"),
        "team": df["TEAM_ABBREVIATION"] if "TEAM_ABBREVIATION" in df
                else df["TEAM_NAME"],
        "team_name": df["TEAM_NAME"].astype("string"),
        "season": df["SEASON"].astype("string"),
        "season_start": df["SEASON_START"].astype("int"),
        "wins_actual": df["W"].astype("int"),
        "losses_actual": df["L"].astype("int"),
        "win_pct": df["W_PCT"].astype("float"),
        "off_rating": df["OFF_RATING"].astype("float"),
        "def_rating": df["DEF_RATING"].astype("float"),
        "net_rating": df["NET_RATING"].astype("float"),
        "pace": df["PACE"].astype("float"),
    })

    out["games"] = out["wins_actual"] + out["losses_actual"]
    # Recompute rather than trusting W_PCT, so games/wins/pct are always coherent.
    out["win_pct"] = out["wins_actual"] / out["games"]
    out["wins_82"] = out["win_pct"] * FULL_SEASON_GAMES
    out["is_shortened"] = out["games"] < FULL_SEASON_GAMES

    return out.sort_values(["season_start", "team_id"]).reset_index(drop=True)


def add_prior_season(df: pd.DataFrame) -> pd.DataFrame:
    """Attach each franchise's previous-season result via an explicit year join.

    Uses ``season_start - 1`` rather than a groupby shift: a shift would silently
    treat a franchise's gap year as consecutive, quietly inventing a prior season
    that does not exist.
    """
    prior = df[["team_id", "season_start", "win_pct", "wins_82", "net_rating"]].copy()
    prior.columns = ["team_id", "season_start", "prev_win_pct", "prev_wins_82",
                     "prev_net_rating"]
    prior["season_start"] += 1
    return df.merge(prior, on=["team_id", "season_start"], how="left")


def summarize(df: pd.DataFrame) -> str:
    lines = [
        f"team-seasons : {len(df)}",
        f"seasons      : {df['season'].nunique()} "
        f"({df['season'].min()} .. {df['season'].max()})",
        f"franchises   : {df['team_id'].nunique()}",
        "",
        "non-82-game seasons:",
    ]
    short = df[df["is_shortened"]]
    for season, grp in short.groupby("season", observed=True):
        counts = sorted(grp["games"].unique())
        note = SHORTENED_SEASONS.get(str(season), "")
        rng = f"{counts[0]}" if len(counts) == 1 else f"{counts[0]}-{counts[-1]}"
        lines.append(f"  {season}: {len(grp)} teams, {rng} games  ({note})")
    return "\n".join(lines)
