"""Player impact metric, calibrated from box-score rates against team results.

## How it works

We never observe a player's individual impact directly -- only team outcomes. So we
let the team results tell us what each box-score stat is worth:

    team offensive rating (vs league avg) ~ minute-weighted avg of player rate stats
    team defensive rating (vs league avg) ~ same

Fit those two ridge regressions, and a player's impact is his own rates run through
the fitted weights. Because the team feature is a *minute-weighted average* of
player features and the model is linear, the identity

    team rating deviation = minute-weighted average of player impacts

holds algebraically -- which is why this formulation was chosen over a metric that
has to be reconciled with team totals after the fact.

**The identity is only as faithful as the regularisation allows.** Regressing actual
team net rating on aggregated impact yields a slope near 5 only when the ridge penalty
is weak. Measured: 5.42 at alpha~0, 5.67 at alpha=1, 7.01 at alpha=10, 10.06 at
alpha=50. Ridge deliberately shrinks coefficients, which compresses the spread of
fitted impacts, which in turn inflates the slope of the target on the prediction.
That is regularisation behaving as designed, not a data defect.

Two earlier explanations for the slope gap were investigated and **disproved**: it is
not caused by players under ``MIN_MINUTES_FOR_RATES`` lacking estimates (covered
minute share is 98.1%, and assigning replacement level to the remainder moved the
slope only 7.73 -> 7.88), and it is not caused by traded-player misattribution.
Recording that here because the wrong cause was written into this file twice.

## The known, important weakness

Box scores describe offense far better than defense. Steals, blocks and defensive
rebounds are a thin description of stopping people: they miss rim deterrence
without a block, positioning, communication, and staying attached off-ball. Expect
the defensive fit to be much weaker than the offensive one -- the report quantifies
it rather than hiding it.

This directly bounds how well we can currently answer the defence-related design
questions (rim protection, on-ball defense). Better defensive estimation needs
either the tracking data (2013-14+ only) or RAPM from play-by-play, both documented
as upgrade paths.

Offense and defense are kept separate throughout rather than collapsed to one
number, because later stages need them independently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

# Rate features. Volume and efficiency both appear on purpose: together they
# distinguish an efficient high-usage scorer from an efficient low-usage one.
# Ridge absorbs the resulting collinearity.
OFFENSE_FEATURES = ["pts_p100", "fg3m_p100", "fga_p100", "fta_p100", "ast_p100",
                    "tov_p100", "oreb_p100", "ts_pct", "fg3_rate"]

# `reb_p100` is deliberately absent: it is exactly oreb + dreb (measured correlation
# 1.000000), and including it alongside both components double-counted rebounding
# into ~10 of the ~14 total defensive weight.
#
# `def_rating_rel` is the on-court/off-court style term, and it is here to fix a real
# failure mode. With box-score stats alone, defensive weight lands almost entirely on
# rebounds and blocks, so the metric ranks every rotation centre above every guard --
# it measures height and role, not defensive skill. (That is the ecological fallacy:
# teams that rebound well genuinely defend well, but an individual's rebound rate
# mostly reflects position.) `def_rating_rel` is position-neutral: it asks whether the
# defense improves when this player is on the floor. It is noisy and confounded by
# which teammates he shares the floor with, which is why it is one shrunk feature
# among several rather than the whole metric.
DEFENSE_FEATURES = ["stl_p100", "blk_p100", "dreb_p100", "pf_p100", "def_rating_rel"]

# Player-tracking defensive features (added when the tracking data is merged in; see
# ``add_tracking_features``). These directly attack the box metric's central defect --
# that ~60% of its defensive weight is defensive rebounds + blocks, so it mostly measures
# *being a center* and hands non-defenders like Karl-Anthony Towns an above-average rating.
#
#   rim_supp  opponent field-goal % at the rim, EXPECTED minus ALLOWED, with this player as
#             the nearest defender (+ = holds shooters below their expected rate). This is
#             the signal a block count misses: deterring or altering a shot without swatting
#             it. It correctly rates KAT below average and Gobert/Wembanyama/Holmgren on top.
#   rim_vol   rim field goals defended per game (how often he is the man at the rim at all).
#   rim_val   rim_supp * rim_vol -- a points-saved proxy that is ~0 for low-volume perimeter
#             players, so "missing" (a guard who never defends the rim) reads as neutral.
#   defl_p36  deflections per 36 minutes -- disruptive perimeter activity (steals that never
#             complete), the on-ball signal blocks/steals under-count.
#   cont2_p36 two-point shots contested per 36 minutes.
#
# Coverage caveat: rim tracking starts 2013-14, hustle 2016-17. Where a feature is missing
# (older seasons, or a player who does not register at the rim) its within-season z-score is
# filled to 0 = league average, so the absence contributes nothing rather than dropping the
# player. Walk-forward these move win MAE only +0.036 (deep in the noise -- every team plays
# centers ~48 min, so the positional bias washes out in aggregate), but they lift the
# defensive calibration r-squared 0.33 -> 0.48 and make the per-player defensive numbers the
# UI shows actually credible. Shipped for that player-level credibility, not for aggregate MAE.
TRACKING_DEFENSE_FEATURES = ["rim_supp", "rim_vol", "rim_val", "defl_p36", "cont2_p36"]

# Features standardized WITHIN position (guard/forward/center) instead of league-wide, so a
# center's value is measured against other centers and a guard's against other guards. Only
# defensive REBOUNDING gets this treatment: it is the one defensive box stat that is mostly
# positional role, not skill (grabbing the ball after a miss), so league-wide it was a 26%-weight
# proxy for "is a center" -- centers averaged +1.03 def_impact, guards −0.33. Blocks, steals, and
# rim protection stay league-wide because they are genuine cross-positional defense: an elite rim
# protector really does prevent more points than a guard, and we must not erase that. Position
# from ``rim_defense`` (2013-14+); pre-tracking or unknown positions fall back to forward, which
# collapses to league-wide standardization for those rows. Walk-forward this is a real win, not
# just a credibility fix: blend win MAE 7.74 -> 7.63, coverage held, AND it lifts the perimeter
# defenders the metric under-rated (Holiday/Anunoby/Dort flip positive) while cutting the
# backup-center over-credit (Neemias Queta +0.24 -> −0.68), with Wembanyama still clearly #1.
#
# `oreb_p100` (offensive rebounding) added 2026-08-06, REPLACING an earlier `ts_pct` attempt --
# it is the offensive mirror of the defensive `dreb_p100` fix above, by the identical logic:
# offensive rebounding is heavily positional ROLE (a center crashes the glass), not shot-making
# skill, so league-wide it inflated centers' offense the same way defensive rebounding inflated
# their defense. Standardizing it within position narrows the center/guard off_impact gap from
# +0.55/−0.57 (a 1.12 spread) to +0.18/−0.44 (0.62) WITHOUT overcorrecting (centers stay slightly
# above guards, as they should), de-inflates pure offensive-rebounding specialists (Steven Adams
# −1.06 -> −1.61, Gobert 1.13 -> 0.74) and PRESERVES genuine offensive-center stars (Jokić stays
# 3.11, vs 2.70 under the old ts_pct). Unlike ts_pct it is a real aggregate win, not just a
# credibility fix: pure-box gate +0.132 wins, 6/6 folds (stable across seeds), roughly double what
# ts_pct gave (+0.064, 3/6); and in the FULL shipped pipeline (offensive RAPM blend on) it adds
# +0.051, 6/6 folds -- exactly where ts_pct collapses to a null (−0.003, 2/6), because the
# offensive RAPM blend (shipped the same day) already corrects the efficiency confound ts_pct
# targeted, but does NOT target the offensive-rebounding ROLE confound oreb removes.
#
# What was tried and rejected (all via scripts/gate_position_relative_offense.py): `ts_pct` (the
# efficiency mirror -- low-usage centers over-credited for near-100% efficiency on assisted rim
# finishes) shipped briefly but is redundant with the offensive RAPM blend, so oreb superseded it;
# `fta_p100` (rim-runner contact-finishing) is a near-no-op (+0.005, barely moves the positional
# gap); `ts_pct + oreb` OVERcorrects (centers drop BELOW guards, only 2 in the top-30 offensive
# leaderboard) and is fold-noisy. fg3m_p100/fg3_rate stay league-wide (centers rarely attempt 3s,
# so there is no positional inflation to remove, and standardizing a near-empty reference group
# risks erasing genuinely earned stretch-5 shooting); pts_p100/fga_p100/tov_p100/fta_p100 are
# usage-driven (a role choice, not anatomy -- Jokić/Embiid prove high usage is available to
# centers); ast_p100 is a passing big's real rare skill, not an opportunity artifact -- all stay
# league-wide.
POSITION_RELATIVE_FEATURES = ["dreb_p100", "oreb_p100"]
ALL_FEATURES = list(dict.fromkeys(OFFENSE_FEATURES + DEFENSE_FEATURES))

# Chosen by DOWNSTREAM projection accuracy, which is the only criterion that matters.
# Correlation between a projected team aggregate and the actual team rating, measured
# walk-forward over 2017-18..2025-26:
#
#     alpha    1 -> 0.595      alpha   10 -> 0.678
#     alpha    5 -> 0.652      alpha   25 -> 0.698   <- chosen (plateaus after)
#                              alpha   60 -> 0.698
#
# This REVERSES an earlier change. Alpha was briefly lowered from 10 to 1 because that
# made the contemporaneous aggregation slope land near its theoretical value of 5. That
# was optimising the wrong thing: matching a slope on same-season data says nothing
# about how well the metric *projects forward*, and the weakly-regularised metric turned
# out to be badly over-dispersed -- projected team aggregates had SD 0.926 against an
# actual 0.829, when a properly shrunken estimator should be narrower than reality, not
# wider. At alpha=25 the relationship is the right way round (0.396 vs 0.448).
#
# The lesson, which this project has now paid for twice: tune against the downstream
# objective, not against an internal diagnostic that merely looks principled.
RIDGE_ALPHA = 25.0

# A player occupies one of five spots on the floor. Because team features are
# minute-weighted *averages* of player features, the fitted linear model satisfies
#     team rating deviation = minute-weighted average of player impacts
# which puts raw impact on a "if all five spots played like him" scale. Dividing by
# five converts to the conventional per-player scale that BPM/RAPM-style metrics use,
# where an All-NBA player sits around +5 to +10 points per 100 possessions.
FLOOR_SPOTS = 5


def add_on_court_features(
    player_seasons: pd.DataFrame,
    player_team_seasons: pd.DataFrame,
    team_seasons: pd.DataFrame,
    player_advanced: pd.DataFrame,
) -> pd.DataFrame:
    """Attach on-court defensive rating relative to the player's own team(s).

    ``player_advanced`` reports the team's defensive rating while this player was on
    the floor. Subtracting his team's overall defensive rating yields an on/off-style
    measure: how much better the defense was with him playing.

    For a traded player the reference point is the *minute-weighted* average of his
    teams' defensive ratings, so a midseason move to a much better defense is not
    mistaken for personal improvement.

    Sign convention: positive is good. Defensive ratings are points allowed, so a
    lower on-court value is better and the difference is negated.
    """
    on_court = pd.DataFrame({
        "player_id": player_advanced["PLAYER_ID"].astype("int64"),
        "season_start": player_advanced["SEASON_START"].astype(int),
        "on_court_def_rating": pd.to_numeric(
            player_advanced["DEF_RATING"], errors="coerce"),
    }).drop_duplicates(["player_id", "season_start"])

    team_def = team_seasons[["team_id", "season_start", "def_rating"]].rename(
        columns={"def_rating": "team_def_rating"})

    stints = player_team_seasons.merge(
        team_def, on=["team_id", "season_start"], how="left")
    stints["wt_def"] = stints["team_def_rating"] * stints["minutes"]
    ref = stints.groupby(["player_id", "season_start"], as_index=False).agg(
        wt_def=("wt_def", "sum"), tot_min=("minutes", "sum"))
    ref["ref_team_def_rating"] = np.where(
        ref["tot_min"] > 0, ref["wt_def"] / ref["tot_min"], np.nan)

    out = player_seasons.merge(on_court, on=["player_id", "season_start"], how="left")
    out = out.merge(ref[["player_id", "season_start", "ref_team_def_rating"]],
                    on=["player_id", "season_start"], how="left")
    out["def_rating_rel"] = -(out["on_court_def_rating"] - out["ref_team_def_rating"])
    return out


# Shot-defense categories beyond the rim, from the same LeagueDashPtDefend endpoint
# (nbaproj.ingest.shot_defense). A 2026-07-31 deep dive proposed extending nearest-defender
# tracking from rim-only to all 6 categories to attack perimeter containment -- the one defensive
# gap tracking still misses. Stage-1 pre-check (year-over-year player stability, min 3
# attempts/game both seasons) killed exactly the zones that would have addressed perimeter
# shooting: 3-pointers r^2=0.002 and >15ft jumpers r^2=0.010, both indistinguishable from noise --
# corroborating rather than fixing "perimeter containment produces few countable events even with
# tracking." "Overall" (r^2=0.041) is likely redundant with rim_supp (dominated by rim-shot volume
# for the same big men) and also dropped. Only two zones survive: 2-pointers (r^2=0.106) and
# less-than-10ft (r^2=0.174) -- both adjacent to the already-covered rim zone, not genuinely new
# perimeter information, but real enough to gate. Column names differ by category (an API quirk):
# {category: (makes_col, attempts_col, expected_pct_col, short_name)}.
SHOT_DEFENSE_SURVIVING_CATEGORIES = {
    "2 Pointers": ("FG2M", "FG2A", "NS_FG2_PCT", "p2"),
    "Less Than 10Ft": ("FGM_LT_10", "FGA_LT_10", "NS_LT_10_PCT", "lt10"),
}
SHOT_DEFENSE_FEATURES = [f"{short}_val" for *_, short in SHOT_DEFENSE_SURVIVING_CATEGORIES.values()]


def add_tracking_features(
    player_seasons: pd.DataFrame,
    rim_defense: pd.DataFrame | None = None,
    hustle: pd.DataFrame | None = None,
    player_team_seasons: pd.DataFrame | None = None,
    shot_defense: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Attach the player-tracking defensive features (``TRACKING_DEFENSE_FEATURES``).

    ``rim_defense`` supplies opponent shooting at the rim against this player as the nearest
    defender; ``hustle`` supplies deflections and contested shots. Both are optional -- when a
    frame is omitted its features are simply absent and later filled to league-average -- so a
    caller with only partial tracking (older seasons) still works. Merged on
    ``(player_id, season_start)``; the rim frame keys the defender on ``CLOSE_DEF_PERSON_ID``.

    ``player_team_seasons``, if supplied, fills ``pos_group`` with BPM's box-derived position
    estimate (see ``_bpm_position_estimate``) wherever the real rim-tracking position is missing
    -- i.e. every player-season before 2013-14, plus the rare post-2013 player rim_defense never
    covers. Without it those rows silently collapse to "F", which is the pre-fix behavior.

    ``shot_defense``, if supplied (``nbaproj.ingest.shot_defense_all_categories``, all 5
    non-rim `LeagueDashPtDefend` categories stacked with a ``DEFENSE_CATEGORY`` column), adds
    ``{short}_val`` for the two categories that survived the stability pre-check (see
    ``SHOT_DEFENSE_SURVIVING_CATEGORIES``); the rest are intentionally not computed.
    """
    out = player_seasons.copy()
    if rim_defense is not None and len(rim_defense):
        rd = rim_defense.rename(columns={"CLOSE_DEF_PERSON_ID": "player_id",
                                         "SEASON_START": "season_start"}).copy()
        # A traded player has one rim row per team stint; combine them into a season total
        # (attempts and makes summed, expected % attempt-weighted) BEFORE deriving rates,
        # so the merge stays strictly one row per player-season.
        rd["_ns_fga"] = rd["NS_LT_06_PCT"] * rd["FGA_LT_06"]
        g = rd.groupby(["player_id", "season_start"], as_index=False).agg(
            fga=("FGA_LT_06", "sum"), fgm=("FGM_LT_06", "sum"),
            games=("G", "sum"), ns_fga=("_ns_fga", "sum"))
        fga = g["fga"].clip(lower=1)
        g["rim_supp"] = (g["ns_fga"] - g["fgm"]) / fga       # expected minus allowed rim FG%
        g["rim_vol"] = g["fga"] / g["games"].clip(lower=1)    # rim FG defended per game
        g["rim_val"] = g["rim_supp"] * g["rim_vol"]           # points-saved proxy
        out = out.merge(g[["player_id", "season_start", "rim_supp", "rim_vol", "rim_val"]],
                        on=["player_id", "season_start"], how="left")
        # Position group (guard/forward/center) for position-relative standardization. One
        # position per player-season; "F-C"/"C-F" -> C, "G-F"/"F-G" -> G by first match.
        if "PLAYER_POSITION" in rd.columns:
            pos = rd.groupby(["player_id", "season_start"], as_index=False)[
                "PLAYER_POSITION"].first()
            pos["pos_group"] = pos["PLAYER_POSITION"].map(_position_group)
            out = out.merge(pos[["player_id", "season_start", "pos_group"]],
                            on=["player_id", "season_start"], how="left")
    if hustle is not None and len(hustle):
        hu = hustle.rename(columns={"PLAYER_ID": "player_id",
                                    "SEASON_START": "season_start"}).copy()
        g = hu.groupby(["player_id", "season_start"], as_index=False).agg(
            defl=("DEFLECTIONS", "sum"), cont2=("CONTESTED_SHOTS_2PT", "sum"),
            mins=("MIN", "sum"))
        mins = g["mins"].clip(lower=1)
        g["defl_p36"] = g["defl"] / mins * 36                 # deflections per 36 min
        g["cont2_p36"] = g["cont2"] / mins * 36               # 2pt shots contested per 36
        out = out.merge(g[["player_id", "season_start", "defl_p36", "cont2_p36"]],
                        on=["player_id", "season_start"], how="left")
    if player_team_seasons is not None and len(player_team_seasons):
        bpm_pos = _bpm_position_estimate(player_team_seasons)
        out = out.merge(bpm_pos, on=["player_id", "season_start"], how="left")
        if "pos_group" in out.columns:
            out["pos_group"] = out["pos_group"].fillna(out["pos_group_bpm"])
        else:
            out["pos_group"] = out["pos_group_bpm"]
        out = out.drop(columns=["pos_group_bpm"])
    if shot_defense is not None and len(shot_defense):
        for cat, (mk, at, expp, short) in SHOT_DEFENSE_SURVIVING_CATEGORIES.items():
            sd = shot_defense[shot_defense["DEFENSE_CATEGORY"] == cat].rename(
                columns={"CLOSE_DEF_PERSON_ID": "player_id", "SEASON_START": "season_start"})
            if sd.empty:
                continue
            sd = sd.copy()
            sd["_ns_fga"] = sd[expp].astype(float) * sd[at].astype(float)
            g = sd.groupby(["player_id", "season_start"], as_index=False).agg(
                fga=(at, "sum"), fgm=(mk, "sum"), games=("G", "sum"), ns_fga=("_ns_fga", "sum"))
            fga = g["fga"].clip(lower=1)
            supp = (g["ns_fga"] - g["fgm"]) / fga             # expected minus allowed
            vol = g["fga"] / g["games"].clip(lower=1)         # attempts defended per game
            g[f"{short}_val"] = supp * vol                    # points-saved-style proxy
            out = out.merge(g[["player_id", "season_start", f"{short}_val"]],
                            on=["player_id", "season_start"], how="left")
    return out


def _position_group(pos: object) -> str:
    """Map a listed position ('C', 'F-C', 'G-F', ...) to a coarse guard/forward/center group.
    Unknown -> forward, which makes position-relative standardization collapse to league-wide."""
    if not isinstance(pos, str):
        return "F"
    return "C" if "C" in pos else ("G" if "G" in pos else "F")


# BPM 2.0's box-derived continuous position estimator (Daniel Myers), used ONLY as a fallback for
# the 8 of 21 backbone seasons (2005-06..2012-13) that have no `rim_defense`-listed position at
# all: pos_group silently collapses to "F" for those seasons -- 34% of the standardization pool --
# so POSITION_RELATIVE_FEATURES quietly stopped correcting the positional confound for a third of
# its training data. Coefficients are BPM's published ones; %TeamX = a player's season total of X
# (from real per-team stints, so a traded player is not double-counted) divided by that team's
# season total. Validated against real listed positions where both exist (2013-14+, 6,819
# player-seasons): correctly separates the extremes (a true guard is bucketed center only 4% of
# the time, a true center bucketed guard 17% of the time) even though the true "forward" middle is
# inherently fuzzy (only ~60% 3-way accuracy) -- which is exactly what matters for telling a
# center's defensive rebounding from a guard's. Cutoffs below are grid-search-optimized on that
# same overlap.
_BPM_POSITION_CUTOFFS = (2.86, 3.33)  # G < 2.86 <= F < 3.33 <= C


def _bpm_position_estimate(player_team_seasons: pd.DataFrame) -> pd.DataFrame:
    """One continuous BPM-style position value per (player_id, season_start), all seasons."""
    team_tot = player_team_seasons.groupby(["team_id", "season_start"], as_index=False).agg(
        t_reb=("reb", "sum"), t_ast=("ast", "sum"), t_stl=("stl", "sum"),
        t_blk=("blk", "sum"), t_pf=("pf", "sum"))
    d = player_team_seasons.merge(team_tot, on=["team_id", "season_start"])
    for c in ["reb", "ast", "stl", "blk", "pf"]:
        d[f"pct_{c}"] = d[c] / d[f"t_{c}"].clip(lower=1)
    d["position_raw"] = (2.130 + 8.668 * d.pct_reb - 2.486 * d.pct_stl
                         + 0.992 * d.pct_pf - 3.536 * d.pct_ast + 1.667 * d.pct_blk
                         ).clip(1, 5)

    # Recursive team recentering: shift every player on a team-season by the same constant so
    # the team's minute-weighted mean position is exactly 3.0 (five spots -> average of 3).
    def _recenter(g: pd.DataFrame) -> pd.Series:
        w = g["minutes"].clip(lower=1)
        shift = 3.0 - np.average(g["position_raw"], weights=w)
        return (g["position_raw"] + shift).clip(1, 5)

    d["position"] = d.groupby(["team_id", "season_start"], group_keys=False).apply(
        _recenter, include_groups=False)

    # Multi-team players: one continuous position per player-SEASON, minutes-weighted across
    # stints (matches how the project already combines a traded player's stint-level rows
    # elsewhere, e.g. rim tracking in this same function).
    out = d.groupby(["player_id", "season_start"]).apply(
        lambda g: np.average(g["position"], weights=g["minutes"].clip(lower=1)),
        include_groups=False).rename("position_est").reset_index()

    lo, hi = _BPM_POSITION_CUTOFFS
    out["pos_group_bpm"] = np.select(
        [out["position_est"] < lo, out["position_est"] >= hi], ["G", "C"], default="F")
    return out[["player_id", "season_start", "pos_group_bpm"]]


def _standardize_within_position(ps: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Minutes-weighted z-score within each (season, position group), for features that are
    positional role more than skill (see ``POSITION_RELATIVE_FEATURES``). Same mechanics as
    ``_standardize_within_season`` but grouped by position too, so a center's rebounding is
    measured against centers. Needs a ``pos_group`` column (from ``add_tracking_features``)."""
    out = ps.copy()
    # Unknown position (pre-tracking seasons, or a player with no rim data) -> forward, so those
    # rows still standardize (as one group) instead of dropping out of the fit entirely.
    grp = (out["pos_group"].fillna("F") if "pos_group" in out.columns
           else pd.Series("F", index=out.index))
    for col in cols:
        out[f"{col}_z"] = np.nan
    for _, sub in out.groupby(["season_start", grp]):
        ok = sub["has_rates"]
        if ok.sum() < 12:
            continue
        w = sub.loc[ok, "minutes"].to_numpy(dtype=float)
        for col in cols:
            v = sub.loc[ok, col].to_numpy(dtype=float)
            good = ~np.isnan(v)
            if good.sum() < 12:
                continue
            mean = np.average(v[good], weights=w[good])
            sd = np.sqrt(np.average((v[good] - mean) ** 2, weights=w[good]))
            if sd > 0:
                z = np.full(len(v), np.nan)
                z[good] = (v[good] - mean) / sd
                out.loc[sub.loc[ok].index, f"{col}_z"] = z
    return out


def _standardize_within_season(ps: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Minutes-weighted z-score within each season, to neutralise era drift.

    Pace rose from ~90 to ~100 possessions per game and three-point attempts more
    than doubled across our window. Without within-season standardisation the model
    would read league-wide change as individual improvement.
    """
    out = ps.copy()
    for col in cols:
        out[f"{col}_z"] = np.nan
    for season, sub in out.groupby("season_start"):
        ok = sub["has_rates"]
        if ok.sum() < 20:
            continue
        w = sub.loc[ok, "minutes"].to_numpy(dtype=float)
        for col in cols:
            v = sub.loc[ok, col].to_numpy(dtype=float)
            good = ~np.isnan(v)
            if good.sum() < 20:
                continue
            mean = np.average(v[good], weights=w[good])
            sd = np.sqrt(np.average((v[good] - mean) ** 2, weights=w[good]))
            if sd > 0:
                z = np.full(len(v), np.nan)
                z[good] = (v[good] - mean) / sd
                out.loc[sub.loc[ok].index, f"{col}_z"] = z
    return out


def build_team_features(
    player_team_seasons: pd.DataFrame,
    player_seasons_z: pd.DataFrame,
    feature_cols: list[str],
) -> pd.DataFrame:
    """Minute-weighted average of player z-scored rates, per team-season.

    Weighted by minutes *with that team*, so a traded player contributes to each of
    his teams in proportion to time actually spent there.
    """
    zcols = [f"{c}_z" for c in feature_cols]
    pz = player_seasons_z[["player_id", "season_start"] + zcols]
    df = player_team_seasons.merge(pz, on=["player_id", "season_start"], how="left")

    rows = []
    for (season, team), sub in df.groupby(["season_start", "team_id"]):
        rec = {"season_start": season, "team_id": team}
        for zc in zcols:
            v = sub[zc].to_numpy(dtype=float)
            w = sub["minutes"].to_numpy(dtype=float)
            good = ~np.isnan(v) & (w > 0)
            # Players below the rate threshold have no z-score; they are excluded
            # from the feature average but their minutes still existed. The share
            # they represent is recorded so the model can see roster churn.
            rec[zc] = np.average(v[good], weights=w[good]) if good.any() else np.nan
            rec["covered_min_share"] = w[good].sum() / w.sum() if w.sum() else np.nan
        rows.append(rec)
    return pd.DataFrame(rows)


def _league_centered_targets(team_seasons: pd.DataFrame) -> pd.DataFrame:
    """Team off/def ratings as deviations from that season's league average.

    Centering within season removes league-wide scoring inflation, which rose
    substantially across the window and is not a property of any team.
    """
    t = team_seasons[["team_id", "season_start", "off_rating", "def_rating",
                      "net_rating"]].copy()
    for col in ("off_rating", "def_rating", "net_rating"):
        t[f"{col}_dev"] = t[col] - t.groupby("season_start")[col].transform("mean")
    # Sign flip: positive should always mean "good", and a low defensive rating is
    # good. Without this the fitted defensive weights read backwards.
    t["def_rating_dev"] = -t["def_rating_dev"]
    return t


def calibrate(
    team_features: pd.DataFrame,
    targets: pd.DataFrame,
    feature_cols: list[str],
    target_col: str,
    alpha: float = RIDGE_ALPHA,
) -> tuple[dict[str, float], float]:
    """Fit ridge weights mapping team features to a team rating deviation.

    Returns (coefficients, in-sample r-squared). Ridge rather than plain least
    squares because the features are deliberately collinear.
    """
    zcols = [f"{c}_z" for c in feature_cols]
    df = team_features.merge(targets, on=["team_id", "season_start"], how="inner")
    df = df.dropna(subset=zcols + [target_col])
    if len(df) < 50:
        raise ValueError(f"only {len(df)} usable team-seasons for calibration")

    X = df[zcols].to_numpy(dtype=float)
    y = df[target_col].to_numpy(dtype=float)
    model = Ridge(alpha=alpha, fit_intercept=True)
    model.fit(X, y)
    r2 = float(model.score(X, y))
    coefs = dict(zip(zcols, model.coef_))
    coefs["__intercept__"] = float(model.intercept_)
    return coefs, r2


def apply_coefs(player_seasons_z: pd.DataFrame, coefs: dict[str, float],
                feature_cols: list[str]) -> pd.Series:
    """Score each player with fitted weights -> impact in points per 100 possessions.

    The intercept is deliberately excluded: it is the league baseline, and including
    it would give every player credit for it. Impact is relative to average.
    """
    zcols = [f"{c}_z" for c in feature_cols]
    X = player_seasons_z[zcols].to_numpy(dtype=float)
    beta = np.array([coefs[c] for c in zcols], dtype=float)
    return pd.Series(X @ beta, index=player_seasons_z.index)


def build_impact(
    player_seasons: pd.DataFrame,
    player_team_seasons: pd.DataFrame,
    team_seasons: pd.DataFrame,
    player_advanced: pd.DataFrame,
    *,
    first_test_season: int = 2013,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk-forward impact estimates plus per-fold calibration diagnostics.

    For each test season the weights are fit only on strictly earlier seasons, so a
    player's impact in season N never depends on how season N turned out.

    Impact columns are returned on the conventional per-player scale (see
    ``FLOOR_SPOTS``): points per 100 possessions relative to a league-average player.
    """
    # Use whatever tracking defensive features the caller merged in (see
    # ``add_tracking_features``); fall back to the pure box set if none are present.
    track = [c for c in TRACKING_DEFENSE_FEATURES + SHOT_DEFENSE_FEATURES
             if c in player_seasons.columns]
    def_features = DEFENSE_FEATURES + track
    all_features = list(dict.fromkeys(OFFENSE_FEATURES + def_features))

    ps = add_on_court_features(
        player_seasons, player_team_seasons, team_seasons, player_advanced)
    # Defensive rebounding is standardized WITHIN position (see POSITION_RELATIVE_FEATURES) when
    # a position column is available, so it stops being a proxy for "is a center"; everything
    # else is standardized league-wide.
    posrel = ([c for c in POSITION_RELATIVE_FEATURES if c in all_features]
              if "pos_group" in ps.columns else [])
    psz = _standardize_within_season(ps, [c for c in all_features if c not in posrel])
    if posrel:
        psz = _standardize_within_position(psz, posrel)
    # A tracking feature is missing for older seasons (before the data existed) and for
    # players who never register at the rim; treat that as league-average (z = 0) rather
    # than dropping the player, but only for real rotation players (`has_rates`), so the
    # aggregation weights tracking exactly as it weights the box features.
    for c in track:
        m = psz["has_rates"]
        psz.loc[m, f"{c}_z"] = psz.loc[m, f"{c}_z"].fillna(0.0)
    targets = _league_centered_targets(team_seasons)
    tf = build_team_features(player_team_seasons, psz, all_features)
    for c in track:  # a team-season with no tracking at all (pre-2013) -> neutral, not dropped
        tf[f"{c}_z"] = tf[f"{c}_z"].fillna(0.0)

    diagnostics, scored = [], []
    for test_season in range(first_test_season,
                             int(player_seasons["season_start"].max()) + 1):
        train_teams = tf[tf["season_start"] < test_season]
        train_targets = targets[targets["season_start"] < test_season]

        off_coefs, off_r2 = calibrate(
            train_teams, train_targets, OFFENSE_FEATURES, "off_rating_dev")
        def_coefs, def_r2 = calibrate(
            train_teams, train_targets, def_features, "def_rating_dev")

        diagnostics.append({
            "test_season": test_season,
            "train_team_seasons": len(train_targets),
            "offense_r2": off_r2,
            "defense_r2": def_r2,
        })

        test = psz[psz["season_start"] == test_season].copy()
        # Divide by FLOOR_SPOTS to report the conventional per-player scale rather
        # than the "all five spots play like him" scale the regression produces.
        test["off_impact"] = apply_coefs(
            test, off_coefs, OFFENSE_FEATURES) / FLOOR_SPOTS
        test["def_impact"] = apply_coefs(
            test, def_coefs, def_features) / FLOOR_SPOTS
        test["impact"] = test["off_impact"] + test["def_impact"]
        scored.append(test)

    return pd.concat(scored, ignore_index=True), pd.DataFrame(diagnostics)
