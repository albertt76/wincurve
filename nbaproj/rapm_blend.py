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


def project_rapm_off(rapm_off_impact: pd.DataFrame, target_season: int) -> pd.Series:
    """Per-player projected RAPM offense for `target_season` -- twin of `project_rapm_def`,
    against an impact table with off_impact swapped to off_rapm (see
    nbaproj.rapm.build_rapm_impact(..., which='off'))."""
    hist = rapm_off_impact[rapm_off_impact["season_start"] < target_season]
    curves = aging_curves(build_transitions(hist, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    proj = project_next_season(hist, curves, target_season=target_season, skill="off_impact")
    return proj.set_index("player_id")["proj_off_impact"]


def top_scorer_share_weight(pts: pd.DataFrame, seasons) -> pd.DataFrame:
    """EXPERIMENTAL offensive blend weight (not shipped): a team's top scorer's share of team
    total points that season, as a proxy for reliance on a single high-usage shot creator --
    the review's proposed mechanism ("weight heaviest for high-usage creators"). Computed from
    `player_team_seasons.pts`, already on disk, no new data. Returns team_id, season_start,
    top_scorer_share, clipped to [0, 1] like the shipped turnover weight."""
    sub = pts[pts["season_start"].isin(list(seasons))]
    team_pts = sub.groupby(["team_id", "season_start"])["pts"].transform("sum")
    share = np.where(team_pts > 0, sub["pts"] / team_pts, 0.0)
    sub = sub.assign(_share=share)
    out = sub.groupby(["team_id", "season_start"], as_index=False)["_share"].max()
    out = out.rename(columns={"_share": "top_scorer_share"})
    out["top_scorer_share"] = out["top_scorer_share"].clip(0.0, 1.0)
    return out


def backtest_aggregates(imp: pd.DataFrame, rapm_imp: pd.DataFrame, pts: pd.DataFrame,
                        pgl: pd.DataFrame, ts: pd.DataFrame, ages: pd.DataFrame,
                        rosters: pd.DataFrame, seasons, *,
                        rapm_off_imp: pd.DataFrame | None = None) -> pd.DataFrame:
    """Walk-forward team aggregates for both arms over `seasons`, blended.

    Runs the decoupled projection twice -- box impact and RAPM-defense impact -- and returns one
    row per team-season with agg_off, agg_def_box, agg_def_rapm, the roster's new_minute_share,
    and the turnover-blended agg_def_used. These are the aggregates the defensive slope is
    calibrated on.

    If `rapm_off_imp` is given (an impact table with off_impact swapped to off_rapm, see
    `nbaproj.rapm.build_rapm_impact(..., which="off")`), a third arm is run and its aggregate is
    returned as `agg_off_rapm`, plus the turnover-blended `agg_off_used` -- the SAME weighting
    formula as the defensive blend (`blend_weight(new_minute_share)`), decided and shipped
    2026-08 after scripts/gate_rapm_offense_blend.py showed every weight variant tried improved
    win MAE (+0.11 to +0.15 wins, majority of folds), including one built from a completely
    different signal (top-scorer's point share) -- that triangulation, not any single variant's
    SE, is what makes this a confident ship. `agg_off` (pure box) is always present and
    unchanged, so existing def-only callers are unaffected.
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

    if rapm_off_imp is not None:
        rapm_off = arm(rapm_off_imp)[["team_id", "season_start", "agg_off"]].rename(
            columns={"agg_off": "agg_off_rapm"})
        A = A.merge(rapm_off, on=["team_id", "season_start"])
        A["agg_off_used"] = (1 - w) * A["agg_off"] + w * A["agg_off_rapm"]
    return A


def calibrate_blend(A: pd.DataFrame, ts: pd.DataFrame, *, target_season: int) -> dict:
    """Walk-forward calibration for the blended model AND its two extremes (box-only, RAPM-only),
    each mapped to team offense/defense with its own slope, so a caller can price a team under
    any of the three methods with a like-for-like fit rather than reusing the blended slope on
    an unblended aggregate.

    Blended: offense on the turnover-blended agg_off_used (falls back to pure-box agg_off if the
    offense RAPM arm was not built, e.g. a caller that never passed rapm_off_imp to
    backtest_aggregates), defense on the turnover-blended agg_def_used. Box-only: agg_off /
    agg_def_box. RAPM-only: agg_off_rapm / agg_def_rapm (only returned when those columns are
    present, i.e. rapm_off_imp was passed to backtest_aggregates).

    Powers the Blended/Box/RAPM method toggle (ui/template.html, ui/nba_players): each method
    needs its own slope because the three aggregates have different scale/dispersion -- reusing
    one slope across methods would repeat the exact mismatch this project's own history warns
    against (fitting a calibration on one kind of quantity and applying it to another).
    """
    off_col = "agg_off_used" if "agg_off_used" in A.columns else "agg_off"
    off_slope, off_int = calibrate_projected_ratings(
        A, ts, target_season=target_season, target="off_rating", agg_col=off_col)
    def_slope, def_int = calibrate_projected_ratings(
        A, ts, target_season=target_season, target="def_rating", agg_col="agg_def_used")
    slope, intercept = calibrate_projected_ratings(A, ts, target_season=target_season)
    off_slope_box, off_int_box = calibrate_projected_ratings(
        A, ts, target_season=target_season, target="off_rating", agg_col="agg_off")
    def_slope_box, def_int_box = calibrate_projected_ratings(
        A, ts, target_season=target_season, target="def_rating", agg_col="agg_def_box")
    out = {"off_slope": off_slope, "off_intercept": off_int,
           "def_slope": def_slope, "def_intercept": def_int,
           "rating_slope": slope, "rating_intercept": intercept,
           "off_slope_box": off_slope_box, "off_intercept_box": off_int_box,
           "def_slope_box": def_slope_box, "def_intercept_box": def_int_box}
    if "agg_off_rapm" in A.columns:
        off_slope_rapm, off_int_rapm = calibrate_projected_ratings(
            A, ts, target_season=target_season, target="off_rating", agg_col="agg_off_rapm")
        out["off_slope_rapm"] = off_slope_rapm
        out["off_intercept_rapm"] = off_int_rapm
    if "agg_def_rapm" in A.columns:
        def_slope_rapm, def_int_rapm = calibrate_projected_ratings(
            A, ts, target_season=target_season, target="def_rating", agg_col="agg_def_rapm")
        out["def_slope_rapm"] = def_slope_rapm
        out["def_intercept_rapm"] = def_int_rapm
    return out


def team_def_blend(agg_def_box: float, agg_def_rapm: float, new_minute_share: float) -> float:
    """The turnover-blended defensive aggregate for one team (mirrors the UI recompute)."""
    w = float(blend_weight(new_minute_share))
    return (1 - w) * agg_def_box + w * agg_def_rapm


def team_off_blend(agg_off_box: float, agg_off_rapm: float, weight: float) -> float:
    """A weighted offensive aggregate for one team, given an already-computed weight. Takes the
    weight explicitly rather than hardcoding it (unlike `team_def_blend`) so a caller can pass
    the shipped turnover weight (`blend_weight(new_minute_share)`, the normal case) or 0/1 for
    the box-only/RAPM-only extremes of the method toggle."""
    w = float(np.clip(weight, 0.0, 1.0))
    return (1 - w) * agg_off_box + w * agg_off_rapm
