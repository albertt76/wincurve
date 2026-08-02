"""Rest-of-season (in-season) projection core.

The shipped projection is preseason-only. Once a season is under way, this re-projects each team's
REMAINING games by shrinking its in-season results toward its preseason projection:

    rest_rating[T] = w(N) * obs_rating[T] + (1 - w(N)) * preseason_prior[T]

which is then simulated over the real remaining schedule. Banked wins add back with zero error.
`w(N)` rises with games played N -- a hot early start is part skill, part luck, so `w < 1` is the
regression to the mean; it is fit walk-forward by minimizing rest-of-season win error.

Gated at N=25 and N=50 (`scripts/gate_inseason_model.py`): rest-of-season win MAE beats
preseason-carried-forward by +0.75 / +0.84 (6/6 folds each) and naive current-pace by +0.43 at
N=25 (5/6); by N=50 the season has spoken and w~0.83 so naive is ~optimal. This module holds the
in-season-specific mechanics; the preseason prior and the simulator come from the existing pipeline
(`nbaproj.rapm_blend`, `nbaproj.carryover`, `nbaproj.simulate`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

# In-season observed rating is a point margin/game; the preseason prior is a per-100 net-rating
# deviation. They coincide to a scale of 1.017 (net_rating = 1.017 * margin/game, corr 0.999),
# so multiply the in-season SRS margin by this to blend on the prior's scale.
PER100 = 1.017

# Default grid for the shrinkage weight w in [0, 1] (0 = pure preseason prior, 1 = pure in-season).
W_GRID = np.round(np.arange(0.0, 1.001, 0.05), 3)


def split_date(gs: pd.DataFrame, n: int) -> pd.Timestamp:
    """Calendar date by which each team has played ~n games: median over teams of the Nth-game date.

    A calendar split (rather than each team's literal Nth game) keeps every remaining game's two
    teams both post-split, so the remaining schedule is coherent for the simulator. ``gs`` is one
    season's game_log rows with a datetime ``GAME_DATE``.
    """
    nth = (gs.sort_values(["TEAM_ID", "GAME_DATE", "GAME_ID"])
           .groupby("TEAM_ID")["GAME_DATE"].nth(n - 1))
    return nth.median()


def _srs(pre: pd.DataFrame, iters: int = 10) -> dict:
    """Simple rating system on the pre-split games only: team margin/game + mean-opponent rating,
    Gauss-Seidel iterated and re-centered to mean 0 (SRS is identified up to a constant)."""
    raw = pre.groupby("TEAM_ID")["PLUS_MINUS"].mean().to_dict()
    opp_lists = pre.groupby("TEAM_ID")["opp_id"].apply(list).to_dict()
    rating = dict(raw)
    for _ in range(iters):
        new = {t: raw[t] + np.mean([rating.get(o, 0.0) for o in opp_lists.get(t, [])] or [0.0])
               for t in raw}
        mu = np.mean(list(new.values()))
        rating = {t: v - mu for t, v in new.items()}
    return rating


def through_split(gs: pd.DataFrame, n: int) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Per-team in-season state at split ``n`` (point-in-time: only games before the split date).

    Returns (state, split_date). ``state`` columns: team_id, obs_rating (schedule-adjusted SRS on
    the prior's net-rating-deviation scale), banked_wins, played, and rem_wins (actual wins in the
    remaining games -- for scoring only, harmless in a live run where it's simply 0).
    """
    g = gs[["GAME_ID", "TEAM_ID", "MATCHUP", "GAME_DATE", "PLUS_MINUS", "WL"]].copy()
    opp = g[["GAME_ID", "TEAM_ID"]].rename(columns={"TEAM_ID": "opp_id"})
    g = g.merge(opp, on="GAME_ID")
    g = g[g["TEAM_ID"] != g["opp_id"]]
    d = split_date(gs, n)
    pre, post = g[g["GAME_DATE"] < d], g[g["GAME_DATE"] >= d]
    srs = _srs(pre)
    banked = pre.groupby("TEAM_ID")["WL"].apply(lambda s: (s == "W").sum())
    rem_wins = post.groupby("TEAM_ID")["WL"].apply(lambda s: (s == "W").sum())
    played = pre.groupby("TEAM_ID")["GAME_ID"].nunique()
    teams = sorted(srs)
    state = pd.DataFrame({
        "team_id": teams,
        "obs_rating": [srs[t] * PER100 for t in teams],
        "banked_wins": [int(banked.get(t, 0)) for t in teams],
        "rem_wins": [int(rem_wins.get(t, 0)) for t in teams],
        "played": [int(played.get(t, 0)) for t in teams],
    })
    return state, d


def expected_wins(ratings: dict, sched: pd.DataFrame, hca: float, msd: float) -> dict:
    """Fast deterministic expected wins per team over ``sched`` -- the simulator's mean, no Monte
    Carlo. Used to fit ``w`` against the downstream win objective cheaply."""
    wins = {t: 0.0 for t in ratings}
    hr = sched["home_id"].map(ratings).to_numpy(dtype=float)
    ar = sched["away_id"].map(ratings).to_numpy(dtype=float)
    gh = np.where(sched["neutral"].to_numpy() if "neutral" in sched else False, 0.0, hca)
    p_home = norm.cdf((hr - ar + gh) / msd)
    for hid, aid, p in zip(sched["home_id"], sched["away_id"], p_home):
        wins[hid] += p
        wins[aid] += 1.0 - p
    return wins


def blend_rating(state: pd.DataFrame, prior: dict, w: float) -> dict:
    """rest_rating = w * obs + (1 - w) * preseason_prior, per team."""
    return {r.team_id: w * r.obs_rating + (1 - w) * prior.get(r.team_id, 0.0)
            for r in state.itertuples()}


def fit_w(train: list[dict], w_grid=W_GRID) -> float:
    """Pick w minimizing rest-of-season win MAE over training seasons.

    Each ``train`` element is a dict with keys: state (through_split frame), prior ({team_id: rating}),
    rem_sched (remaining schedule pairs), hca, msd. Fitting on the deterministic expected-wins
    objective (not a rating-space proxy) keeps w aligned with the downstream win metric while staying
    cheap enough to run per fold.
    """
    best_w, best_mae = float(w_grid[len(w_grid) // 2]), np.inf
    for w in w_grid:
        errs = []
        for t in train:
            rating = blend_rating(t["state"], t["prior"], w)
            ew = expected_wins(rating, t["rem_sched"], t["hca"], t["msd"])
            errs += [abs(ew[r.team_id] - r.rem_wins) for r in t["state"].itertuples()]
        mae = float(np.mean(errs))
        if mae < best_mae:
            best_mae, best_w = mae, w
    return best_w
