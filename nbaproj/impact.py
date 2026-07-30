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
    ps = add_on_court_features(
        player_seasons, player_team_seasons, team_seasons, player_advanced)
    psz = _standardize_within_season(ps, ALL_FEATURES)
    targets = _league_centered_targets(team_seasons)
    tf = build_team_features(player_team_seasons, psz, ALL_FEATURES)

    diagnostics, scored = [], []
    for test_season in range(first_test_season,
                             int(player_seasons["season_start"].max()) + 1):
        train_teams = tf[tf["season_start"] < test_season]
        train_targets = targets[targets["season_start"] < test_season]

        off_coefs, off_r2 = calibrate(
            train_teams, train_targets, OFFENSE_FEATURES, "off_rating_dev")
        def_coefs, def_r2 = calibrate(
            train_teams, train_targets, DEFENSE_FEATURES, "def_rating_dev")

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
            test, def_coefs, DEFENSE_FEATURES) / FLOOR_SPOTS
        test["impact"] = test["off_impact"] + test["def_impact"]
        scored.append(test)

    return pd.concat(scored, ignore_index=True), pd.DataFrame(diagnostics)
