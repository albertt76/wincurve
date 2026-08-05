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

import numpy as np
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


# Year-over-year persistence of GSAx/60 (2011-2024, 1500-min starters): corr ~0.13, slope ~0.15.
# Goaltending is nearly unpredictable season to season -- far below skater offense (~0.62) -- so a
# forward projection regresses GSAx ~85% toward league average (0). Consequence: projected
# goaltending barely differentiates teams; the skater aggregation carries the projection, even
# though realized goalie variance swings standings. (Stage 4/5: a multi-season base is a refinement.)
GSAX_PERSISTENCE = 0.15


def project_gsax(end_year: int, *, min_toi: float = 1500.0,
                 beta: float = GSAX_PERSISTENCE) -> pd.DataFrame:
    """Projected next-season GSAx per 60 per goalie = ``beta * this-season GSAx/60`` (heavy
    regression toward 0, since goalie performance barely persists). Floored at ``min_toi`` minutes.
    """
    g = goalie_gsax("all")
    g = g[(g["season_start"] == end_year) & (g["toi_min"] >= min_toi)].copy()
    g["proj_gsax_per_60"] = beta * g["gsax_per_60"]
    return g[["player_id", "name", "toi_min", "gsax_per_60", "proj_gsax_per_60"]].reset_index(drop=True)


# MoneyPuck's legacy dotted team codes (through 2019-20) -> NHL tricodes; see rapm.MP_CODE_ALIASES.
_MP_GOALIE_ALIASES = {"L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL"}


def team_gsax_per_game(end_year: int, *, beta: float = GSAX_PERSISTENCE) -> pd.DataFrame:
    """Projected team goaltending, as goals-against SAVED per game, for season ``end_year + 1``.

    Point-in-time: each goalie's **prior season (``end_year``)** GSAx/60 is regressed toward 0
    (``beta``, since goaltending barely persists) and attributed to his prior-season team, weighted
    by that season's share of the team's goalie minutes -- so a team's projected save value is the
    minute-weighted mean of its returning goalies' regressed GSAx/60 (backups project to ~0). This
    is the goals-against adjustment the season simulation subtracts from the skater-based GA rate;
    it is deliberately small (best starters ~+0.1 goals/game), because goalie value is nearly
    unpredictable in advance. Goalie trades/UFA moves are missed (Y-team-of-record), a known
    refinement. Returns ``(team, gsax_pg)`` -- positive = saves goals = lowers projected GA.
    """
    g = pd.read_parquet(ingest.PROC / "moneypuck_goalies.parquet")
    g = g[(g["situation"] == "all") & (g["season_start"] == end_year)].copy()
    g["team"] = g["team"].replace(_MP_GOALIE_ALIASES)
    g["player_id"] = g["playerId"].astype(int)
    g["gsax60"] = (g["xGoals"].astype(float) - g["goals"].astype(float)) / (g["icetime"] / 60.0) * 60.0
    # minute-weighted mean GSAx/60 over each team's goalies, then regress toward 0
    g["w"] = g["icetime"]
    agg = g.groupby("team").apply(
        lambda d: np.average(d["gsax60"], weights=d["w"]), include_groups=False).rename("gsax60")
    out = agg.reset_index()
    out["gsax_pg"] = beta * out["gsax60"]        # goals saved per 60 ~= per game (starter ~full game)
    return out[["team", "gsax_pg"]]
