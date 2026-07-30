"""Stage 1 baselines -- the bar every later stage has to clear.

Nothing in this project counts as progress until it beats **mean-reverted previous
wins**. That baseline is deceptively strong: NBA team quality is highly
autocorrelated, so "last year, pulled toward .500" already captures most of the
predictable signal in a season record. Published models that look impressive often
fail to beat it.

All fitting is walk-forward: to predict season N we use only seasons < N. The
reversion coefficient is *learned per fold*, never fit on the full history, so the
reported error is genuinely out-of-sample.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .teams import FULL_SEASON_GAMES, add_prior_season

LEAGUE_MEAN_PCT = 0.5  # exact by construction: every game has one winner

# Number of games each season's preseason win total was actually set for. Verified
# from the league-average total: 2011-12 averages 33.1 (a 66-game schedule) and
# 2020-21 averages 36.0 (72 games), while every other season averages ~41.
#
# 2019-20 is deliberately 82: that season began on a normal schedule and was
# interrupted afterwards, so the totals were set for 82 games even though only
# 64-75 were played. That makes its market comparison apples-to-oranges, so it is
# reported separately rather than silently mixed in (see MARKET_SUSPECT_SEASONS).
MARKET_TOTAL_GAMES = {"2011-12": 66, "2020-21": 72}
MARKET_SUSPECT_SEASONS = {"2019-20"}


def market_wins_82(odds_df: pd.DataFrame) -> pd.Series:
    """Rescale each season's win total to an 82-game equivalent."""
    scale_games = odds_df["season"].map(MARKET_TOTAL_GAMES).fillna(
        FULL_SEASON_GAMES).astype(float)
    return odds_df["wins_ou"] * FULL_SEASON_GAMES / scale_games


def fit_reversion(train: pd.DataFrame) -> float:
    """Least-squares reversion coefficient k in pct_next = .5 + k*(pct_prev - .5).

    k = 1 is pure persistence, k = 0 predicts .500 for everyone. NBA sits around
    0.6-0.7 -- teams keep most but not all of their quality year to year.
    """
    x = train["prev_win_pct"] - LEAGUE_MEAN_PCT
    y = train["win_pct"] - LEAGUE_MEAN_PCT
    ok = x.notna() & y.notna()
    if ok.sum() < 30:
        return 1.0
    x, y = x[ok].to_numpy(), y[ok].to_numpy()
    return float(x @ y / (x @ x))


def walk_forward(df: pd.DataFrame, first_test_season: int = 2013) -> pd.DataFrame:
    """Predict each season from prior seasons only; return per-team predictions.

    Yields one row per (team, test season) with both baselines, in win_pct and in
    82-game-equivalent wins.
    """
    df = add_prior_season(df)
    rows = []

    for test_season in range(first_test_season, int(df["season_start"].max()) + 1):
        train = df[df["season_start"] < test_season]
        test = df[df["season_start"] == test_season]
        if test.empty:
            continue

        k = fit_reversion(train)

        for _, r in test.iterrows():
            if pd.isna(r["prev_win_pct"]):
                continue
            persist = r["prev_win_pct"]
            reverted = LEAGUE_MEAN_PCT + k * (r["prev_win_pct"] - LEAGUE_MEAN_PCT)
            rows.append({
                "season": r["season"],
                "season_start": test_season,
                "team": r["team"],
                "team_id": r["team_id"],
                "k_fitted": k,
                "actual_pct": r["win_pct"],
                "pred_persist_pct": persist,
                "pred_reverted_pct": reverted,
                "actual_wins_82": r["win_pct"] * FULL_SEASON_GAMES,
                "pred_persist_wins": persist * FULL_SEASON_GAMES,
                "pred_reverted_wins": reverted * FULL_SEASON_GAMES,
            })

    return pd.DataFrame(rows)


def score(preds: pd.DataFrame) -> pd.DataFrame:
    """MAE / RMSE per baseline, in 82-game-equivalent wins."""
    out = []
    for label, col in [
        ("previous wins (persistence)", "pred_persist_wins"),
        ("mean-reverted previous wins", "pred_reverted_wins"),
        ("always .500 (41 wins)", None),
    ]:
        if col is None:
            pred = np.full(len(preds), 0.5 * FULL_SEASON_GAMES)
        else:
            pred = preds[col].to_numpy()
        err = preds["actual_wins_82"].to_numpy() - pred
        out.append({
            "baseline": label,
            "MAE_wins": np.abs(err).mean(),
            "RMSE_wins": np.sqrt((err**2).mean()),
        })
    return pd.DataFrame(out)


def noise_floor(df: pd.DataFrame) -> dict[str, float]:
    """Estimate the irreducible error floor.

    Even a model that knew each team's exact true strength would miss, because an
    82-game record is a finite sample from it. For a team with true strength p,
    the binomial SD of the record is sqrt(82*p*(1-p)) wins -- about 4.5 at p=.5.

    This is the number that makes the whole project interpretable: it is why a
    ~5-win MAE is close to as good as anyone can do, and why the market being
    hard to beat is expected rather than surprising.
    """
    p = df["win_pct"].to_numpy()
    p = p[~np.isnan(p)]
    sd_per_team = np.sqrt(FULL_SEASON_GAMES * p * (1 - p))
    sd = float(sd_per_team.mean())
    return {
        # For a normal error with SD s, E|error| = s*sqrt(2/pi).
        "binomial_MAE_wins": sd * np.sqrt(2 / np.pi),
        "binomial_RMSE_wins": sd,
        "observed_wins_82_SD": float(np.nanstd(df["win_pct"] * FULL_SEASON_GAMES)),
    }
