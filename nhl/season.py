"""Stage 5/6 projection pipeline: projected skaters -> team goal rates -> POINTS distribution.

The reusable core shared by the Stage 5 gate (`scripts/nhl_stage5_sim_report.py`) and the live
upcoming-season projection (`scripts/nhl_project_current.py`). It holds the walk-forward panel,
the strength->goals calibration, the season simulation mean, and the one-year carryover -- so both
the backtest and the production projection run the *identical* pipeline and cannot drift.

Everything is point-in-time safe: a season Y's projection uses only data from before Y (projected
impacts from `project(Y-1)`, an opening-day roster, prior-season minutes, calibration + carryover
fit on strictly-earlier seasons, and the prior season's league scoring level for goal drift).
"""

from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

from . import aggregate, gamesim, projection, rosters
from .ingest import PROC, season_id

REF = pd.read_parquet(PROC / "team_reference.parquet")[["team_id", "tricode"]]


def team_actuals(Y: int) -> pd.DataFrame:
    """Actual per-team goals-for/against per game and 82-game-equivalent points for season Y."""
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)].merge(REF, left_on="teamId", right_on="team_id")
    ts["pts82"] = ts["points"] / (2 * ts["gamesPlayed"]) * 164.0
    return ts.rename(columns={"tricode": "team", "goalsForPerGame": "gf",
                              "goalsAgainstPerGame": "ga"})[["team", "gf", "ga", "pts82"]]


def league_gf(Y: int) -> float:
    """League mean goals-for per team per game in season Y (the next season's scoring-level est.)."""
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    return float(ts[ts["seasonId"] == season_id(Y)]["goalsForPerGame"].mean())


def projectable_seasons(first: int = 2011, last: int = 2025) -> list[int]:
    """Seasons Y that have both a pooled-RAPM projection input (an impact cache in [Y-3..Y-1]) and
    an opening-day roster (`shifts_Y`) -- the seasons the honest backtest panel can cover."""
    alpha = int(projection.rapm.DEFAULT_ALPHA)
    imp = {int(Path(f).stem.split("_")[1]) for f in glob.glob(str(PROC / f"impact_*_a{alpha}.parquet"))}
    shifts = {int(Path(f).stem.split("_")[1]) for f in glob.glob(str(PROC / "shifts_*.parquet"))}
    return [Y for Y in range(first, last + 1)
            if any(s in imp for s in range(Y - 3, Y)) and Y in shifts]


def build_panel(years: list[int], *, honest: bool = True) -> pd.DataFrame:
    """Projected 5v5 off/def/net (honest opening-day roster + prior TOI) + actual gf/ga/pts82."""
    rows = []
    for Y in years:
        toi = rosters.honest_toi(Y) if honest else None
        g = aggregate.team_ratings(projection.project(Y - 1), Y, toi=toi)
        rows.append(g[["team", "off", "def", "net"]].merge(team_actuals(Y), on="team").assign(Y=Y))
    return pd.concat(rows, ignore_index=True)


def calibrate(train: pd.DataFrame, level: float) -> dict:
    """Fit the strength->goals calibration on `train` (rows strictly before the target season).

    Slopes relate projected 5v5 off/def to actual goals-for/against per game; the *level* (intercept)
    is the prior season's league goals/game, so scoring drift (2.66->3.08 over the window) is tracked
    from data known before the target -- never fit on it. Also returns the shipped linear net->pts82
    for the A/B baseline.
    """
    return {
        "a1": float(np.polyfit(train["off"], train["gf"], 1)[0]),   # off  -> goals-for/game
        "b1": float(np.polyfit(train["def"], train["ga"], 1)[0]),   # def  -> goals-against/game
        "level": level,
        "lin": np.polyfit(train["net"], train["pts82"], 1),         # (slope, intercept) net->pts82
    }


def goal_rates(off: np.ndarray, dfn: np.ndarray, cal: dict) -> tuple[np.ndarray, np.ndarray]:
    """Projected (goals-for, goals-against) per game from off/def, centered on the scoring level.

    Centering within the projected team set forces the projected league-mean GF to equal `level`
    (mean of the deviations is 0), so the absolute scoring level is the prior season's, not the
    training years' -- drift-safe and leak-free.
    """
    off = np.asarray(off, dtype=float)
    dfn = np.asarray(dfn, dtype=float)
    gf = cal["level"] + cal["a1"] * (off - off.mean())
    ga = cal["level"] + cal["b1"] * (dfn - dfn.mean())
    return gf, ga


def sim_mean(off: np.ndarray, dfn: np.ndarray, cal: dict) -> np.ndarray:
    """Closed-form expected 82-game points per team from the goal-based game model."""
    gf, ga = goal_rates(off, dfn, cal)
    return gamesim.expected_points(gf, ga, league_gf=cal["level"])


def walkforward_means(P: pd.DataFrame) -> pd.DataFrame:
    """Fill each panel season's shipped-linear and goal-sim projected points (`lin`, `mu`) and the
    sim's season-luck variance (`luckvar`), each calibrated on strictly-earlier seasons only."""
    P = P.copy()
    P["lin"] = np.nan
    P["mu"] = np.nan
    P["luckvar"] = np.nan
    for s in sorted(P["Y"].unique()):
        tr = P[P["Y"] < s]
        if len(tr) < 10:
            continue
        te = P["Y"] == s
        sub = P[te]
        cal = calibrate(tr, league_gf(s - 1))
        P.loc[te, "lin"] = cal["lin"][1] + cal["lin"][0] * sub["net"]
        gf, ga = goal_rates(sub["off"].values, sub["def"].values, cal)
        P.loc[te, "mu"] = gamesim.expected_points(gf, ga, league_gf=cal["level"])
        samp = gamesim.simulate_points(gf, ga, league_gf=cal["level"], n_sims=4000,
                                       rng=np.random.default_rng(11))
        P.loc[te, "luckvar"] = samp.var(axis=0)
    return P


def carryover(P: pd.DataFrame, Y: int, col: str = "mu") -> tuple[float, pd.Series]:
    """(rho, per-team carry) for the projection in `col`: rho * (team's season Y-1 residual), rho fit
    on residual pairs strictly before Y. `P` must carry `<col>_resid` (actual pts82 - projection)."""
    rcol = col + "_resid"
    pr = P[P["Y"] < Y].merge(
        P.assign(Y=P["Y"] + 1)[["team", "Y", rcol]].rename(columns={rcol: "rp"}),
        on=["team", "Y"]).dropna(subset=[rcol, "rp"])
    rho = float(np.polyfit(pr["rp"], pr[rcol], 1)[0]) if len(pr) > 10 else 0.0
    prev = P[P["Y"] == Y - 1].set_index("team")[rcol]
    idx = P[P["Y"] == Y]["team"]
    return rho, (idx.map(prev).fillna(0.0) * rho).set_axis(idx.values)


def projection_sigma(P: pd.DataFrame, Y: int) -> float:
    """Total predictive points SD for season Y's interval = SD of the sim+carry residual on PRIOR
    folds (so the interval width matches the post-carryover error it is centered on)."""
    prior = P[P["Y"] < Y].dropna(subset=["muc_resid"])
    if len(prior) > 10:
        return float(prior["muc_resid"].std())
    return float(P[P["Y"] == Y]["muc_resid"].std())


def add_carry_residual(P: pd.DataFrame) -> pd.DataFrame:
    """Add the walk-forward sim+carry prediction (`mu_carry`) and its residual (`muc_resid`) for
    every panel season -- the basis for the interval width in `projection_sigma`."""
    P = P.copy()
    P["mu_resid"] = P["pts82"] - P["mu"]
    P["mu_carry"] = np.nan
    for s in sorted(P["Y"].unique()):
        sub = P[P["Y"] == s]
        if sub["mu"].isna().all():
            continue
        _, carry = carryover(P, s, "mu")
        P.loc[P["Y"] == s, "mu_carry"] = sub["mu"].values + sub["team"].map(carry).fillna(0.0).values
    P["muc_resid"] = P["pts82"] - P["mu_carry"]
    return P
