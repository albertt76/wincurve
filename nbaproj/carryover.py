"""One-year residual carryover: the model persistently under- or over-rates a team, and
that error carries forward one season.

## What this is, and what it is NOT

Across a franchise's history our team-rating residuals are autocorrelated at about +0.35
from one season to the next. Four independent analyses plus their adversarial verifiers
converged on what that means:

- It is **one-year memory**, not a permanent franchise trait. The autocorrelation decays
  like AR(1) (lags 1-4: +0.35, +0.12, -0.10, ~0) and is essentially gone by year three. The
  "permanent franchise effect" has a point estimate of zero.
- So a 9-season franchise mean is the WRONG estimator (it assumes a constant level and was
  worth ~0.04 wins walk-forward). A one-season carryover is the RIGHT one, and end-to-end
  through the simulation it was worth roughly +0.30 wins.
- Part of it is our defensive blind spot (~70% of the residual is defensive) and part is
  short-lived roster/valuation error. The carryover captures whatever persists for a year
  without needing to name the cause.

## The mechanism

For target season N, add to each team's projected rating a shrunk multiple of its most
recent residual:

    adjusted_pred[N] = pred[N] + rho * residual[N-1]
    residual[N-1]    = actual_rating_dev[N-1] - pred[N-1]

`rho` is fitted walk-forward on all consecutive residual pairs from seasons strictly before
N, so the adjustment for season N never sees season N. This is legitimately available at
projection time: season N-1 is already played, and pred[N-1] was our own walk-forward
projection for it.

## The shortened-season guard

A residual computed from an interrupted season is unreliable. 2019-20 (bubble, 64-75 games
that varied by team) is the clearest case, and a verifier found the carryover loses ~1.2
wins on the 2020-21 fold specifically because its prior season is the bubble. So the
carryover is suppressed whenever the prior season was shortened, rather than trusting a
residual built on a distorted sample.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .teams import SHORTENED_SEASONS

# Seasons whose residual is too distorted to carry forward. Keyed by START year.
_SHORTENED_STARTS = {int(s[:4]) for s in SHORTENED_SEASONS}


def _season_dev(team_seasons: pd.DataFrame, target: str = "net_rating") -> pd.DataFrame:
    """Actual within-season rating deviation, the quantity predictions are compared to."""
    t = team_seasons[["team_id", "season_start", target]].copy()
    t["actual_dev"] = t[target] - t.groupby("season_start")[target].transform("mean")
    return t[["team_id", "season_start", "actual_dev"]]


def fit_rho(predictions: pd.DataFrame, team_seasons: pd.DataFrame,
            *, before_season: int) -> float:
    """Least-squares carryover coefficient on residual pairs from seasons < before_season.

    `predictions` must carry `team_id`, `season_start`, and `pred_net_rating_dev` for a
    contiguous run of walk-forward predictions.

    A pair (N-1, N) is used only if BOTH seasons precede `before_season` and N-1 was a
    full-length season; otherwise the residual being regressed on is untrustworthy.
    """
    dev = _season_dev(team_seasons)
    p = predictions.merge(dev, on=["team_id", "season_start"], how="inner")
    p = p.dropna(subset=["pred_net_rating_dev", "actual_dev"])
    p["residual"] = p["actual_dev"] - p["pred_net_rating_dev"]

    prev = p[["team_id", "season_start", "residual"]].rename(
        columns={"residual": "prev_residual"})
    prev["season_start"] += 1
    pairs = p.merge(prev, on=["team_id", "season_start"], how="inner")

    pairs = pairs[
        (pairs["season_start"] < before_season)
        & (~(pairs["season_start"] - 1).isin(_SHORTENED_STARTS))
    ]
    if len(pairs) < 40:
        return 0.0

    x = pairs["prev_residual"].to_numpy(dtype=float)
    y = pairs["residual"].to_numpy(dtype=float)
    # Through-origin least squares: a zero prior residual should imply zero adjustment.
    denom = float(x @ x)
    return float(x @ y / denom) if denom > 0 else 0.0


def apply_carryover(target_predictions: pd.DataFrame,
                    all_predictions: pd.DataFrame,
                    team_seasons: pd.DataFrame,
                    *, target_season: int, rho: float | None = None) -> pd.DataFrame:
    """Return `target_predictions` with the carryover added to `pred_net_rating_dev`.

    `all_predictions` supplies the walk-forward predictions used to form last season's
    residual (it must include `target_season - 1`). `rho` is fitted here if not supplied.

    If the prior season was shortened, or there is no prior prediction for a team (an
    expansion or first-modelled team), that team's adjustment is zero.
    """
    if rho is None:
        rho = fit_rho(all_predictions, team_seasons, before_season=target_season)

    out = target_predictions.copy()
    out["carryover"] = 0.0
    if rho == 0.0 or (target_season - 1) in _SHORTENED_STARTS:
        out["pred_before_carryover"] = out["pred_net_rating_dev"]
        return out

    dev = _season_dev(team_seasons)
    prev = all_predictions[all_predictions["season_start"] == target_season - 1]
    prev = prev.merge(dev, on=["team_id", "season_start"], how="left")
    prev["prev_residual"] = prev["actual_dev"] - prev["pred_net_rating_dev"]
    carry = dict(zip(prev["team_id"], prev["prev_residual"]))

    out["carryover"] = rho * out["team_id"].map(carry).fillna(0.0)
    out["pred_before_carryover"] = out["pred_net_rating_dev"]
    out["pred_net_rating_dev"] = out["pred_net_rating_dev"] + out["carryover"]
    return out
