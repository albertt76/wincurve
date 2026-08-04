"""Goaltending value: GSAx (goals saved above expected).

Goaltending is hockey's separate, volatile module -- there is no NBA analog. A goalie's
value is how many goals he prevents versus what an average goalie would allow on the same
shots: **GSAx = expected goals against (xG on shots faced) - actual goals against**.
Positive = saved more than expected = good.

Computed from MoneyPuck's goalie season summaries (``xGoals`` and ``goals`` on unblocked
shots faced), overall and at even strength. Reported as a total and as a per-60-minute
rate, which is the form the team projection will age and shrink -- separately from skaters,
because goalie performance is far less stable year to year (a projection trap to respect,
not a number to trust at face value in small samples).
"""

from __future__ import annotations

import pandas as pd

from . import ingest


def goalie_gsax(situation: str = "all") -> pd.DataFrame:
    """Per goalie-season GSAx from MoneyPuck.

    ``situation`` in {all, 5on5, 5on4, 4on5}. Returns one row per (goalie, season) with
    the expected/actual goals against, GSAx, and GSAx per 60 minutes.
    """
    g = pd.read_parquet(ingest.PROC / "moneypuck_goalies.parquet")
    g = g[g["situation"] == situation].copy()

    out = pd.DataFrame({
        "player_id": g["playerId"].astype(int),
        "season_start": g["season_start"].astype(int),
        "name": g["name"],
        "games": g["games_played"].astype(int),
        "toi_min": g["icetime"] / 60.0,
        "xga": g["xGoals"].astype(float),      # expected goals against (on shots faced)
        "ga": g["goals"].astype(float),        # actual goals against
    })
    out["gsax"] = out["xga"] - out["ga"]                     # saved above expected
    out["gsax_per_60"] = out["gsax"] / out["toi_min"] * 60.0
    return out.sort_values(["season_start", "gsax"], ascending=[True, False]).reset_index(drop=True)
