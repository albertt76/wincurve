"""Availability: how much of next season each player is actually there for.

This stage exists because availability is roughly co-equal with talent in the error
budget. A projection that nails talent and misses 900 minutes of playing time is
wrong by about as much as one that gets the minutes right and the talent wrong.

## What we can and cannot measure

We derive games missed from box-score absences via nba_api. That is ToS-clean and
gives the signal that matters -- how much a player was available -- but it loses the
*reason*. Injury reason carries real forward signal (soft-tissue and knee problems
recur; a broken finger does not), and the best historical source for it,
Pro Sports Transactions, sits behind a Cloudflare bot challenge we will not bypass.
So the model below is a pure availability-history model, and that is a known ceiling
on it rather than a design preference.

Absences here also mix injury with rest, suspension, trades and coach's decision.
We cannot separate them, so "availability" is deliberately the name rather than
"health".

## The age interaction

A specific hypothesis worth testing rather than assuming: injury history should be
more predictive for older players than younger ones. `evaluate` fits the model with
and without an age x history interaction so the claim is settled by measurement.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

# Lookback for availability history. Three seasons is a compromise: one is too noisy
# to separate a chronic problem from one unlucky year, and beyond three the player is
# a materially different athlete.
LOOKBACK = 3


def team_games_by_season(team_seasons: pd.DataFrame) -> pd.DataFrame:
    """Games each team actually played, so availability is a fair share.

    Necessary rather than cosmetic: dividing by a flat 82 would mark every player in
    2011-12 (66 games) or 2020-21 (72) as chronically unavailable.
    """
    return team_seasons[["team_id", "season_start", "games"]].rename(
        columns={"games": "team_games"})


def build_availability(player_team_seasons: pd.DataFrame,
                       team_seasons: pd.DataFrame) -> pd.DataFrame:
    """Availability rate per player-season: share of his team's games he played.

    For a player who changed teams midseason, the denominator is the sum of games
    played by each team while he was there, which is approximated by his stint
    minutes share. A traded player should not be penalised for the fact that no
    single team played all of his games.
    """
    tg = team_games_by_season(team_seasons)
    stints = player_team_seasons.merge(tg, on=["team_id", "season_start"], how="left")

    agg = stints.groupby(["player_id", "season_start"], as_index=False).agg(
        player_name=("player_name", "first"),
        games=("games", "sum"),
        minutes=("minutes", "sum"),
        n_teams=("team_id", "nunique"),
        team_games=("team_games", "max"),
    )
    agg["availability"] = (agg["games"] / agg["team_games"]).clip(upper=1.0)
    agg["min_per_game"] = np.where(
        agg["games"] > 0, agg["minutes"] / agg["games"], 0.0)
    return agg


def build_features(avail: pd.DataFrame, impact: pd.DataFrame) -> pd.DataFrame:
    """Assemble point-in-time features: prior availability, age, role.

    Every feature uses only seasons strictly before the one being predicted.
    """
    ages = impact[["player_id", "season_start", "age"]].drop_duplicates()
    df = avail.merge(ages, on=["player_id", "season_start"], how="left")
    df = df.sort_values(["player_id", "season_start"])

    g = df.groupby("player_id")
    # shift(1) is what makes these point-in-time: they describe the seasons before.
    for lag in range(1, LOOKBACK + 1):
        df[f"avail_lag{lag}"] = g["availability"].shift(lag)
        df[f"mpg_lag{lag}"] = g["min_per_game"].shift(lag)

    lags = [f"avail_lag{i}" for i in range(1, LOOKBACK + 1)]
    df["avail_hist"] = df[lags].mean(axis=1)
    df["avail_hist_min"] = df[lags].min(axis=1)
    # Trend: is availability heading down? Negative means deteriorating.
    df["avail_trend"] = df["avail_lag1"] - df["avail_lag3"]
    df["mpg_hist"] = df[[f"mpg_lag{i}" for i in range(1, LOOKBACK + 1)]].mean(axis=1)
    df["age_prev"] = g["age"].shift(1)
    return df


FEATURES = ["avail_lag1", "avail_lag2", "avail_hist", "avail_hist_min",
            "avail_trend", "mpg_hist", "age_prev"]
INTERACTION = "age_x_hist"


def _prep(df: pd.DataFrame, with_interaction: bool) -> tuple[np.ndarray, list[str]]:
    cols = list(FEATURES)
    work = df.copy()
    if with_interaction:
        # Centred so the interaction captures "history matters more when older"
        # rather than smuggling in a plain age effect.
        work[INTERACTION] = (work["age_prev"] - 27.0) * (work["avail_hist"] - 0.75)
        cols.append(INTERACTION)
    return work[cols].to_numpy(dtype=float), cols


def evaluate(features: pd.DataFrame, *, first_test_season: int = 2010
             ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk-forward availability prediction, with and without the age interaction.

    Baselines are deliberately included. Predicting "last season's availability" or
    even "the league average" is a real bar here: availability is much noisier and
    less persistent than talent, and a model that cannot beat a flat league average
    is not worth carrying.
    """
    df = features.dropna(subset=FEATURES + ["availability"]).copy()
    rows, coefs = [], []

    for test_season in range(first_test_season,
                             int(df["season_start"].max()) + 1):
        train = df[df["season_start"] < test_season]
        test = df[df["season_start"] == test_season]
        if len(train) < 200 or test.empty:
            continue

        league_mean = train["availability"].mean()
        rec = {"season": test_season, "n": len(test),
               "MAE_league_mean": (test["availability"] - league_mean).abs().mean(),
               "MAE_last_season": (test["availability"]
                                   - test["avail_lag1"]).abs().mean()}

        for label, inter in (("model", False), ("model_interaction", True)):
            Xtr, cols = _prep(train, inter)
            Xte, _ = _prep(test, inter)
            model = Ridge(alpha=1.0)
            model.fit(Xtr, train["availability"].to_numpy(dtype=float))
            pred = np.clip(model.predict(Xte), 0.0, 1.0)
            rec[f"MAE_{label}"] = float(
                np.abs(test["availability"].to_numpy() - pred).mean())
            if inter:
                coefs.append({"season": test_season,
                              **dict(zip(cols, model.coef_))})
        rows.append(rec)

    return pd.DataFrame(rows), pd.DataFrame(coefs)


def fit_predict(features: pd.DataFrame, *, target_season: int,
                with_interaction: bool = True) -> pd.DataFrame:
    """Fit on everything before `target_season` and predict availability for it."""
    df = features.dropna(subset=FEATURES + ["availability"])
    train = df[df["season_start"] < target_season]
    if train.empty:
        raise ValueError(f"no training data before {target_season}")

    Xtr, cols = _prep(train, with_interaction)
    model = Ridge(alpha=1.0)
    model.fit(Xtr, train["availability"].to_numpy(dtype=float))

    # Predict for players whose *history* ends the season before the target.
    hist = features[features["season_start"] == target_season - 1].copy()
    hist["avail_lag1"] = hist["availability"]
    hist["avail_lag2"] = hist["avail_lag1"]
    hist["age_prev"] = hist["age"]
    hist = hist.dropna(subset=FEATURES)
    if hist.empty:
        return pd.DataFrame()

    Xte, _ = _prep(hist, with_interaction)
    hist["proj_availability"] = np.clip(model.predict(Xte), 0.0, 1.0)
    hist["season_start"] = target_season
    return hist[["player_id", "player_name", "season_start", "age",
                 "proj_availability", "mpg_hist"]]
