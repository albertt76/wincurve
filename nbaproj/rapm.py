"""Regularized Adjusted Plus-Minus (RAPM): offensive and defensive player value from
stint data, isolating each player from his nine on-court peers.

## The idea

Across a season's segments (constant-lineup intervals from nbaproj.pbp), regress the score
margin per 100 possessions on indicators for who was on the floor. Ridge regularisation is
essential: with ~450 players and heavy collinearity (teammates share the floor), ordinary
least squares is hopeless, but a ridge penalty shrinks unproven players toward zero and
stabilises the estimate. That penalty is literally where the "regularized" in RAPM comes
from.

## Offensive vs defensive split

Each segment contributes two rows, so offense and defense are estimated separately:

- Row A (home has the ball): the five home players get +1 in their OFFENSE columns, the five
  away players +1 in their DEFENSE columns. Response = home points scored per 100 poss.
- Row B (away has the ball): mirror image. Response = away points per 100.

A player's OFFENSE coefficient is points added on offense; his DEFENSE coefficient is points
allowed, so a *negative* defensive coefficient is good. `defensive_rapm` flips the sign so
that, as everywhere else in this project, positive means good.

## Why this is worth the trouble

The box-score metric captures ~12-14% of franchise-level defensive variance. RAPM sees
defense directly -- it does not care whether a stop came from a block or from perfect
positioning that forced a miss -- so it is the documented fix for the model's defensive
blind spot. The cost is the data: stint reconstruction over thousands of games.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.linear_model import Ridge

HOME_COLS = [f"home_p{i}" for i in range(1, 6)]
AWAY_COLS = [f"away_p{i}" for i in range(1, 6)]

# Ridge strength in RAPM is a real prior, not a nuisance: it is the number of "league-average
# possessions" of evidence a player must accumulate before his estimate moves off zero.
# ~2000 is a standard single-season choice; tune by cross-validation on held-out segments.
DEFAULT_ALPHA = 2000.0


def box_vs_rapm_by_player(impact: pd.DataFrame, rapm_dir: str | Path, *,
                          last_season: int, alpha: int = 2000,
                          first_season: int = 2013) -> dict[int, dict]:
    """Per-player {box_def, def_rapm, season_start} from the most recent season, at or before
    `last_season`, where both a box-score `def_impact` and a cached box-informed RAPM defense
    exist. Used to surface where our box defense disagrees with play-by-play RAPM.

    RAPM is read from ``<rapm_dir>/rapm_<season>_a<alpha>.parquet``. Only seasons that were
    actually pulled contribute; a player with none is simply absent from the result (no flag).
    Passing ``last_season = target - 1`` keeps the comparison walk-forward.
    """
    box = impact[["player_id", "season_start", "def_impact"]].rename(
        columns={"def_impact": "box_def"})
    frames = []
    for s in range(first_season, last_season + 1):
        f = Path(rapm_dir) / f"rapm_{s}_a{alpha}.parquet"
        if f.exists():
            rf = pd.read_parquet(f)[["player_id", "def_rapm"]].copy()
            rf["season_start"] = s
            frames.append(rf)
    if not frames:
        return {}
    both = (pd.concat(frames, ignore_index=True)
            .merge(box, on=["player_id", "season_start"], how="inner").dropna()
            .sort_values("season_start").groupby("player_id").tail(1))
    return both.set_index("player_id")[
        ["box_def", "def_rapm", "season_start"]].to_dict("index")


def build_rapm_impact(impact: pd.DataFrame, rapm_dir: str | Path, *,
                      alpha: int = 2000, which: str = "def") -> pd.DataFrame:
    """A copy of the impact table with box-score off_impact and/or def_impact replaced by
    box-informed RAPM wherever a cached season estimate exists (`impact` recomputed as
    off + def).

    Box-informed RAPM is anchored on the box prior, so the two are on the same scale and the
    swap is well-posed. Players/seasons without a RAPM estimate keep their box value.

    `which` selects which side(s) to swap: "def" (the shipped defensive-blend arm), "off"
    (the offensive-blend experiment -- `off_rapm` is fit by the same estimator but was
    previously discarded here), or "both".
    """
    out = impact.copy()
    swap_def = which in ("def", "both")
    swap_off = which in ("off", "both")
    new_def = out["def_impact"].to_numpy(dtype=float).copy()
    new_off = out["off_impact"].to_numpy(dtype=float).copy()
    for s in sorted(int(x) for x in out["season_start"].unique()):
        f = Path(rapm_dir) / f"rapm_{s}_a{alpha}.parquet"
        if not f.exists():
            continue
        rf = pd.read_parquet(f).set_index("player_id")
        mask = (out["season_start"] == s).to_numpy()
        rows = np.where(mask)[0]
        pids = out.loc[mask, "player_id"]
        if swap_def and "def_rapm" in rf.columns:
            mapped = pids.map(rf["def_rapm"]).to_numpy(dtype=float)
            take = ~np.isnan(mapped)
            new_def[rows[take]] = mapped[take]
        if swap_off and "off_rapm" in rf.columns:
            mapped = pids.map(rf["off_rapm"]).to_numpy(dtype=float)
            take = ~np.isnan(mapped)
            new_off[rows[take]] = mapped[take]
    if swap_def:
        out["def_impact"] = new_def
    if swap_off:
        out["off_impact"] = new_off
    out["impact"] = out["off_impact"] + out["def_impact"]
    return out


def build_design(segments: pd.DataFrame) -> tuple[sparse.csr_matrix, np.ndarray,
                                                  np.ndarray, list[int]]:
    """Build the offense/defense design matrix from segments.

    Returns (X, y, weights, players):
      - X is sparse with 2*n_players columns: [offense_0..offense_{n-1},
        defense_0..defense_{n-1}].
      - y is points scored by the offense per 100 possessions for that row.
      - weights are possessions (so long segments count more).
      - players is the column-order list of player ids; offense column j and defense column
        n+j both refer to players[j].
    """
    seg = segments[segments["poss"] > 0].reset_index(drop=True)
    if seg.empty:
        return sparse.csr_matrix((0, 0)), np.zeros(0), np.zeros(0), []

    players = sorted(pd.unique(seg[HOME_COLS + AWAY_COLS].to_numpy().ravel()))
    idx = {p: j for j, p in enumerate(players)}
    n = len(players)

    home = np.column_stack([seg[c].map(idx).to_numpy() for c in HOME_COLS])
    away = np.column_stack([seg[c].map(idx).to_numpy() for c in AWAY_COLS])

    # Points scored by each side during the segment. home_margin = home_pts - away_pts; we
    # need the components, reconstructed from margin and the tempo estimate is not enough --
    # so build_segments must also carry home_pts/away_pts. When only margin is present, fall
    # back to splitting it (offense row = +margin/2 baseline); prefer explicit points.
    if "home_pts" in seg.columns and "away_pts" in seg.columns:
        home_pts = seg["home_pts"].to_numpy(dtype=float)
        away_pts = seg["away_pts"].to_numpy(dtype=float)
    else:
        # Degrade gracefully: treat the margin as the offensive signal on each side.
        half = seg["home_margin"].to_numpy(dtype=float)
        home_pts = np.clip(half, 0, None)
        away_pts = np.clip(-half, 0, None)

    poss = seg["poss"].to_numpy(dtype=float)
    per100 = 100.0 / np.where(poss > 0, poss, 1.0)

    rows_i, cols_i, vals = [], [], []
    y, w = [], []
    r = 0
    for s in range(len(seg)):
        # Row A: home offense vs away defense.
        for j in range(5):
            rows_i += [r, r]
            cols_i += [home[s, j], n + away[s, j]]
            vals += [1.0, 1.0]
        y.append(home_pts[s] * per100[s])
        w.append(poss[s])
        r += 1
        # Row B: away offense vs home defense.
        for j in range(5):
            rows_i += [r, r]
            cols_i += [away[s, j], n + home[s, j]]
            vals += [1.0, 1.0]
        y.append(away_pts[s] * per100[s])
        w.append(poss[s])
        r += 1

    X = sparse.csr_matrix((vals, (rows_i, cols_i)), shape=(r, 2 * n))
    return X, np.array(y), np.array(w), players


def fit_rapm(segments: pd.DataFrame, *, alpha: float = DEFAULT_ALPHA,
             prior: pd.DataFrame | None = None) -> pd.DataFrame:
    """Fit offensive and defensive RAPM. One row per player, in points per 100 possessions.

    `off_rapm` is points added on offense; `def_rapm` is sign-flipped so positive is good
    defense; `rapm` is their sum, the player's net on-court value.

    ## Box-informed prior (the fix for small-sample noise)

    Plain ridge shrinks every player toward zero. On a partial or single season that
    over-shrinks elite specialists whose value needs many possessions to emerge -- measured
    directly here, it rated Wembanyama's defense at +1.2 when the box score (correctly)
    said +4.3. Shrinking toward a **box-score prior** instead of toward zero fixes this:
    a player with few possessions stays near his box estimate, and only accumulates
    evidence to move off it.

    `prior` is a frame with columns player_id, off_prior, def_prior (both points per 100,
    def positive = good, matching def_rapm's convention). The trick is a change of
    variables: writing beta = beta_prior + delta turns the penalty ||beta - beta_prior||^2
    into an ordinary ridge on delta with response y - X @ beta_prior. So we residualise the
    target against the prior, ridge the remainder, and add the prior back.
    """
    X, y, w, players = build_design(segments)
    if X.shape[0] == 0 or not players:
        return pd.DataFrame(columns=["player_id", "off_rapm", "def_rapm", "rapm",
                                     "poss"])

    n = len(players)
    # Centre the response so the intercept carries the league baseline and coefficients are
    # deviations from average, matching how the box-score impact metric is defined.
    y_mean = np.average(y, weights=w)
    target = y - y_mean

    beta_prior = np.zeros(2 * n)
    if prior is not None and not prior.empty:
        pmap = prior.set_index("player_id")

        def _num(v: object) -> float:
            # A player with no box estimate that season (below the minutes threshold) has
            # NaN here; his prior is zero, i.e. plain shrinkage toward average for him.
            f = float(v) if v is not None else 0.0
            return 0.0 if np.isnan(f) else f

        for j, p in enumerate(players):
            if p in pmap.index:
                row = pmap.loc[p]
                beta_prior[j] = _num(row.get("off_prior", 0.0))       # offense column
                beta_prior[n + j] = -_num(row.get("def_prior", 0.0))  # points allowed
        # Residualise the target against the prior; ridge estimates only the deviation.
        target = target - X @ beta_prior

    model = Ridge(alpha=alpha, fit_intercept=False)
    model.fit(X, target, sample_weight=w)
    beta = model.coef_ + beta_prior

    # Possessions played per player, for a minutes-style weight downstream.
    seg = segments[segments["poss"] > 0]
    poss_by = {}
    for _, s in seg.iterrows():
        for c in HOME_COLS + AWAY_COLS:
            poss_by[s[c]] = poss_by.get(s[c], 0.0) + s["poss"]

    return pd.DataFrame({
        "player_id": [int(p) for p in players],
        "off_rapm": beta[:n],
        # Defensive coefficient is points ALLOWED; negate so positive = good defense.
        "def_rapm": -beta[n:],
        "rapm": beta[:n] - beta[n:],
        "poss": [poss_by.get(p, 0.0) for p in players],
    }).sort_values("rapm", ascending=False).reset_index(drop=True)
