"""Stage 1 baselines -- the bar every later NHL stage has to clear.

Nothing counts as progress until it beats **mean-reverted previous points**, the
hockey analog of the NBA project's mean-reverted-wins bar. All fitting is walk-forward:
to predict season N we use only seasons < N, and the reversion coefficient is learned
per fold.

One structural difference from basketball to keep honest: the league-average points
percentage is NOT 0.5. The overtime "loser point" awards 3 total points in a game
decided past regulation (2 to the winner, 1 to the loser) instead of 2, so the league
mean point_pct sits around 0.56. Reversion is therefore centered on the *training-set*
mean point_pct, not on a hard-coded 0.5.

Errors are reported in **82-game-equivalent points** (point_pct * 164), lower better.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .teams import FULL_SEASON_POINTS, add_prior_season


def fit_reversion(train: pd.DataFrame, mean_pct: float) -> float:
    """Least-squares k in pct_next = mean + k*(pct_prev - mean), centered on the mean.

    k = 1 is pure persistence; k = 0 predicts league-average for everyone.
    """
    x = train["prev_point_pct"].astype(float) - mean_pct
    y = train["point_pct"].astype(float) - mean_pct
    ok = x.notna() & y.notna()
    if ok.sum() < 30:
        return 1.0
    x, y = x[ok].to_numpy(), y[ok].to_numpy()
    return float(x @ y / (x @ x))


def walk_forward(df: pd.DataFrame, first_test_season: int = 2010) -> pd.DataFrame:
    """Predict each season from prior seasons only; one row per (team, test season)."""
    df = add_prior_season(df)
    rows = []
    for test_season in range(first_test_season, int(df["season_start"].max()) + 1):
        train = df[df["season_start"] < test_season]
        test = df[df["season_start"] == test_season]
        if test.empty or train.empty:
            continue

        mean_pct = float(train["point_pct"].mean())  # ~0.56, not 0.5 (loser point)
        k = fit_reversion(train, mean_pct)

        for _, r in test.iterrows():
            if pd.isna(r["prev_point_pct"]):
                continue  # expansion debut -- no prior season
            prev = float(r["prev_point_pct"])
            reverted = mean_pct + k * (prev - mean_pct)
            rows.append({
                "season": r["season"], "season_start": test_season,
                "team": r["team"], "fid": r["fid"],
                "k_fitted": k, "train_mean_pct": mean_pct,
                "actual_pct": r["point_pct"],
                "pred_persist_pct": prev,
                "pred_reverted_pct": reverted,
                "pred_mean_pct": mean_pct,
                "actual_points_82": r["point_pct"] * FULL_SEASON_POINTS,
            })
    return pd.DataFrame(rows)


def score(preds: pd.DataFrame) -> pd.DataFrame:
    """MAE / RMSE per baseline, in 82-game-equivalent points."""
    out = []
    for label, col in [
        ("previous points (persistence)", "pred_persist_pct"),
        ("mean-reverted previous points  <- THE BAR", "pred_reverted_pct"),
        ("league-average points (flat)", "pred_mean_pct"),
    ]:
        pred = preds[col].to_numpy() * FULL_SEASON_POINTS
        err = preds["actual_points_82"].to_numpy() - pred
        out.append({
            "baseline": label,
            "MAE_points": np.abs(err).mean(),
            "RMSE_points": np.sqrt((err**2).mean()),
        })
    return pd.DataFrame(out)


def noise_floor(df: pd.DataFrame) -> dict[str, float]:
    """Approximate the irreducible error floor, in 82-game points.

    APPROXIMATE and an UPPER bound: it models each game as awarding 2*Bernoulli(p)
    points (a regulation win/loss), ignoring the loser point. The loser point makes
    real outcomes a 0/1/2 trinomial with LOWER variance, so the true floor is a bit
    below this. Reported only for interpretability (why ~a few points of MAE is close
    to the achievable frontier); it is never a target. A proper trinomial floor is a
    later refinement.
    """
    p = df["point_pct"].to_numpy()
    p = p[~np.isnan(p)]
    sd_per_team = 2.0 * np.sqrt(82 * p * (1 - p))  # points SD over 82 games, approx
    sd = float(sd_per_team.mean())
    return {
        "approx_binomial_MAE_points": sd * np.sqrt(2 / np.pi),
        "approx_binomial_RMSE_points": sd,
        "observed_points_82_SD": float(np.nanstd(df["point_pct"] * FULL_SEASON_POINTS)),
    }
