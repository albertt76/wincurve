"""RAPM defensive blend: put play-by-play RAPM defense into the projection where it helps most.

The box-score defensive metric is the model's weakest link (~60% of its weight is defensive
rebounds + blocks, so it mostly measures *being a center*). RAPM (regularized adjusted
plus/minus) from play-by-play sees perimeter defense the box cannot. Blending the two
*defensive team aggregates*, weighted by roster turnover, is the shippable integration:

    agg_def_used = (1 - w) * agg_def_box  +  w * agg_def_rapm,   w = new-minute share

The box metric plus the one-year carryover already handle a *stable* roster's defense; RAPM's
value attaches to *players*, so it travels across the roster churn the carryover cannot follow.
Walk-forward this improves win MAE by +0.19 (all folds, `scripts/gate_rapm_blend.py`), better
than pure box or pure RAPM, with equal-or-better interval coverage -- and it lives in the one
decoupled pipeline (no second projection arm), since offense and def are already separate.

Everything here is walk-forward: the RAPM arm only ever uses RAPM from seasons before the
target (via `project_team_ratings`' own `hist < target` gate), and the defensive slope is
calibrated on the blended aggregate from earlier folds only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .aging import aging_curves, build_transitions, project_next_season
from .project import calibrate_projected_ratings, project_team_ratings
from .simulate import roster_turnover


def blend_weight(new_minute_share: pd.Series | float):
    """Weight on the RAPM defensive aggregate = the team's new-minute share, clipped to [0, 1].
    A steady roster leans on the box metric (which the carryover already corrects); a
    turned-over roster leans on RAPM, whose player-level value follows the incoming players."""
    return np.clip(new_minute_share, 0.0, 1.0)


def project_rapm_def(rapm_impact: pd.DataFrame, target_season: int) -> pd.Series:
    """Per-player projected RAPM defense for `target_season`, aged exactly like box defense
    (own aging curve, from seasons before the target). Returns a player_id-indexed Series."""
    hist = rapm_impact[rapm_impact["season_start"] < target_season]
    curves = aging_curves(build_transitions(hist, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    proj = project_next_season(hist, curves, target_season=target_season, skill="def_impact")
    return proj.set_index("player_id")["proj_def_impact"]


def backtest_aggregates(imp: pd.DataFrame, rapm_imp: pd.DataFrame, pts: pd.DataFrame,
                        pgl: pd.DataFrame, ts: pd.DataFrame, ages: pd.DataFrame,
                        rosters: pd.DataFrame, seasons) -> pd.DataFrame:
    """Walk-forward team aggregates for both arms over `seasons`, blended.

    Runs the decoupled projection twice -- box impact and RAPM-defense impact -- and returns one
    row per team-season with agg_off, agg_def_box, agg_def_rapm, the roster's new_minute_share,
    and the turnover-blended agg_def_used. These are the aggregates the defensive slope is
    calibrated on.
    """
    def arm(impact):
        out = []
        for s in seasons:
            r = project_team_ratings(impact, pts, pgl, ts, ages, target_season=s,
                                     mode="roster", team_rosters=rosters, decouple=True)
            if not r.empty:
                out.append(r)
        return pd.concat(out, ignore_index=True)

    box = arm(imp).rename(columns={"agg_def": "agg_def_box"})
    rapm = arm(rapm_imp)[["team_id", "season_start", "agg_def"]].rename(
        columns={"agg_def": "agg_def_rapm"})
    A = box.merge(rapm, on=["team_id", "season_start"])
    turn = pd.concat([roster_turnover(pts, season_start=s).assign(season_start=s)
                      for s in seasons], ignore_index=True)
    A = A.merge(turn[["team_id", "season_start", "new_minute_share"]],
                on=["team_id", "season_start"], how="left")
    A["new_minute_share"] = A["new_minute_share"].fillna(0.3)
    w = blend_weight(A["new_minute_share"])
    A["agg_def_used"] = (1 - w) * A["agg_def_box"] + w * A["agg_def_rapm"]
    return A


def calibrate_blend(A: pd.DataFrame, ts: pd.DataFrame, *, target_season: int) -> dict:
    """Walk-forward calibration for the blended model: offense on agg_off, defense on the
    turnover-blended agg_def_used, each mapped to team offense/defense with its own slope.
    Returns the four coefficients plus the combined slope (display-only)."""
    off_slope, off_int = calibrate_projected_ratings(
        A, ts, target_season=target_season, target="off_rating", agg_col="agg_off")
    def_slope, def_int = calibrate_projected_ratings(
        A, ts, target_season=target_season, target="def_rating", agg_col="agg_def_used")
    slope, intercept = calibrate_projected_ratings(A, ts, target_season=target_season)
    return {"off_slope": off_slope, "off_intercept": off_int,
            "def_slope": def_slope, "def_intercept": def_int,
            "rating_slope": slope, "rating_intercept": intercept}


def team_def_blend(agg_def_box: float, agg_def_rapm: float, new_minute_share: float) -> float:
    """The turnover-blended defensive aggregate for one team (mirrors the UI recompute)."""
    w = float(blend_weight(new_minute_share))
    return (1 - w) * agg_def_box + w * agg_def_rapm
