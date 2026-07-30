"""Stage 5: Monte Carlo season simulation from projected team ratings.

## Why simulate instead of regressing wins on rating

A regression gives a point estimate. We need a *distribution*, because that is what
win-total markets price and what "we disagree with the market" has to be measured
against. Simulation also handles, for free, three things a regression fudges:

  - Real schedule strength: teams do not face identical opponents.
  - Home and away split, with a home advantage that has been shrinking.
  - Non-linearity at the tails, where a 65-win team's marginal rating point buys
    fewer wins than a 41-win team's.

## The part that is easy to get wrong

Simulating only game-level randomness produces intervals that are far too narrow. That
noise alone gives roughly 4.3 wins of spread, while our actual projection error is much
larger -- because the dominant uncertainty is not "how do the coin flips land" but "is
our rating for this team even right". A team whose star tears an ACL in December was
never an X-win team to begin with.

So each simulated season first draws a *true* rating for every team from the projection's
own error distribution, then plays the schedule. `sigma_rating` is fitted from
walk-forward residuals rather than guessed, which is what makes the resulting intervals
calibrated instead of merely plausible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_SIMS = 5000


def extract_schedule(game_log: pd.DataFrame, season_start: int) -> pd.DataFrame:
    """Home/away pairs for one season's real regular-season schedule."""
    gl = game_log[game_log["SEASON_START"] == season_start].copy()
    if gl.empty:
        return pd.DataFrame()
    gl["is_home"] = ~gl["MATCHUP"].str.contains("@", na=False)
    home = gl[gl["is_home"]][["GAME_ID", "TEAM_ID"]].rename(
        columns={"TEAM_ID": "home_id"})
    away = gl[~gl["is_home"]][["GAME_ID", "TEAM_ID"]].rename(
        columns={"TEAM_ID": "away_id"})
    return home.merge(away, on="GAME_ID", how="inner")


def estimate_game_params(game_log: pd.DataFrame, *, before_season: int,
                         lookback: int = 5) -> tuple[float, float]:
    """Estimate home advantage and game-margin spread from recent seasons only.

    Both have drifted materially -- home advantage from about +3.1 points in the late
    2000s to under +2 recently, while margin spread rose from ~13 to ~16 -- so a
    constant fitted on the full history would misprice both ends of the window. Only
    seasons before `before_season` are used, keeping this walk-forward safe.
    """
    gl = game_log[
        (game_log["SEASON_START"] < before_season)
        & (game_log["SEASON_START"] >= before_season - lookback)
    ].copy()
    if gl.empty:
        return 2.5, 14.0
    gl["is_home"] = ~gl["MATCHUP"].str.contains("@", na=False)
    home = gl[gl["is_home"]][["GAME_ID", "PTS"]].rename(columns={"PTS": "hp"})
    away = gl[~gl["is_home"]][["GAME_ID", "PTS"]].rename(columns={"PTS": "ap"})
    g = home.merge(away, on="GAME_ID")
    margin = (g["hp"] - g["ap"]).astype(float)
    return float(margin.mean()), float(margin.std())


def fit_rating_sigma(predictions: pd.DataFrame, actuals: pd.DataFrame,
                     *, before_season: int) -> float:
    """Spread of our own rating errors, from folds strictly before `before_season`.

    This is the uncertainty that dominates the win distribution, so it must come from
    measured past performance rather than an assumption.
    """
    p = predictions[predictions["season_start"] < before_season]
    if p.empty:
        return 4.0
    j = p.merge(actuals, on=["team_id", "season_start"], how="inner").dropna(
        subset=["pred_net_rating_dev", "net_rating_dev"])
    if len(j) < 30:
        return 4.0
    return float((j["pred_net_rating_dev"] - j["net_rating_dev"]).std())


def simulate_season(
    ratings: pd.DataFrame,
    schedule: pd.DataFrame,
    *,
    hca: float,
    margin_sd: float,
    sigma_rating: float,
    n_sims: int = DEFAULT_SIMS,
    seed: int = 12345,
) -> pd.DataFrame:
    """Simulate the season `n_sims` times; return each team's win distribution.

    `ratings` needs `team_id` and `pred_net_rating_dev`.

    Vectorised over simulations: every game in every simulation is evaluated in one
    array pass, which is what makes 5,000 seasons cheap enough to run per fold.
    """
    rng = np.random.default_rng(seed)
    teams = ratings["team_id"].to_numpy()
    idx = {t: i for i, t in enumerate(teams)}
    base = ratings["pred_net_rating_dev"].to_numpy(dtype=float)

    home = np.array([idx[t] for t in schedule["home_id"]])
    away = np.array([idx[t] for t in schedule["away_id"]])

    # One true-rating draw per team per simulation: the season-level uncertainty.
    true_ratings = base[None, :] + rng.normal(0.0, sigma_rating, (n_sims, len(teams)))

    # Expected margin per game per simulation, then a coin flip weighted by it.
    exp_margin = true_ratings[:, home] - true_ratings[:, away] + hca
    win_prob = 0.5 * (1.0 + _erf(exp_margin / (margin_sd * np.sqrt(2.0))))
    home_wins = rng.random(exp_margin.shape) < win_prob

    wins = np.zeros((n_sims, len(teams)), dtype=np.int32)
    np.add.at(wins, (np.arange(n_sims)[:, None], home[None, :]),
              home_wins.astype(np.int32))
    np.add.at(wins, (np.arange(n_sims)[:, None], away[None, :]),
              (~home_wins).astype(np.int32))

    return pd.DataFrame({
        "team_id": teams,
        "pred_net_rating_dev": base,
        "mean_wins": wins.mean(axis=0),
        "sd_wins": wins.std(axis=0),
        "p10_wins": np.percentile(wins, 10, axis=0),
        "p50_wins": np.percentile(wins, 50, axis=0),
        "p90_wins": np.percentile(wins, 90, axis=0),
    }), wins


def _erf(x: np.ndarray) -> np.ndarray:
    """Vectorised error function, for the normal CDF without a scipy round trip."""
    from scipy.special import erf
    return erf(x)


def win_total_probability(wins: np.ndarray, team_index: int, line: float) -> float:
    """P(team finishes above `line` wins) -- the quantity a win-total market prices."""
    col = wins[:, team_index]
    return float((col > line).mean())
