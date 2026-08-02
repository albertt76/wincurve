"""Gate for the NEW rest-of-season (in-season) projection model.

The shipped model is PRESEASON-only. This new model re-projects a team's REMAINING games once
the season is under way (run points: ~25 games, and ~50 games post trade deadline), by shrinking
its in-season results toward its preseason projection:

    rest_rating[T] = w(N) * obs_rating[T] + (1 - w(N)) * preseason_prior[T]

then simulating the REAL remaining schedule from that rating. Banked wins add back with zero error.
w(N) rises with games played N (a hot 25-game start is part skill, part luck -- w<1 is the
regression to the mean). It is fit WALK-FORWARD (per fold, on prior seasons only) by directly
minimizing rest-of-season win error (a fast deterministic expected-wins objective -- NOT a
rating-space proxy, which has misled this project before), then EVALUATED with the full 5000-sim.

Scored as REST-OF-SEASON MAE: mean |predicted remaining wins - actual remaining wins|. (Full-season
MAE is reported for continuity but is NOT the headline -- it mechanically collapses as banked share
grows.) The model must beat BOTH baselines at N=25 and N=50:
    (i)  preseason-carried-forward = w=0 (pure preseason prior, simulated over the remaining games)
    (ii) naive current pace        = w=1 (pure in-season obs rating, simulated over the remaining games)

Design notes (from the scoping pass):
- Calendar split d_N(S) = median over teams of each team's Nth-game date, so every remaining game
  has both teams post-split (coherent for the sim). Point-in-time: obs uses only games before d_N.
- obs_rating = SRS (simple rating system: team margin/game + mean opponent SRS, iterated) over the
  pre-split games only, x1.017 to land on the preseason prior's per-100 net_rating_dev scale.
- preseason_prior = the shipped walk-forward pred_net_rating_dev (backtest_aggregates -> calibrate
  -> carryover), reused verbatim.
- Two per-fold scalars w25/w50 (too few seasons for a free curve). Shortened seasons (2019,2020 and
  the carryover-tainted 2021) excluded, mirroring the shipped gate. v1 is TEAM-RESULTS only;
  per-player in-season talent update is a documented v2.

    python scripts/gate_inseason_model.py
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.inseason import blend_rating, fit_w, through_split  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season)
from nbaproj.teams import FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
FIRST, LAST, N_SIMS = 2017, 2025, 5000
SPLITS = (25, 50)
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}          # {2019, 2020}


# --- preseason prior (reuse the shipped walk-forward pipeline) ----------------

def _load_common():
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    gl["GAME_DATE"] = pd.to_datetime(gl["GAME_DATE"])
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ages = pd.DataFrame({"player_id": pa["PLAYER_ID"].astype("int64"),
                         "season_start": pa["SEASON_START"].astype(int),
                         "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()
    return imp, pts, pgl, gl, pa, ts, rosters, ages


def build_prior_frame(imp, pts, pgl, ts, ages, rosters):
    """cal: per-team preseason pred_net_rating_dev (pre-carryover), + actual2 for sigma fitting."""
    rapm_imp = build_rapm_impact(imp, PROC)
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, range(2016, LAST + 1))
    cal = []
    for s in sorted(A.season_start.unique()):
        so, io = calibrate_projected_ratings(A, ts, target_season=s, target="off_rating",
                                             agg_col="agg_off")
        sd, idc = calibrate_projected_ratings(A, ts, target_season=s, target="def_rating",
                                              agg_col="agg_def_used")
        sub = A[A.season_start == s].copy()
        sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub.agg_def_used + idc
        cal.append(sub)
    cal = pd.concat(cal, ignore_index=True)
    actual2 = ts.assign(net_rating_dev=ts.net_rating
                        - ts.groupby("season_start").net_rating.transform("mean"))
    return cal, actual2


def prior_for(cal, ts, s):
    """Preseason prior (with carryover) for season s -> {team_id: pred_net_rating_dev}."""
    sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
    return dict(zip(sub.team_id, sub.pred_net_rating_dev))


# --- gate --------------------------------------------------------------------

def _sim_wins(rating_map, sched, hca, msd, sigma, seed, teams):
    r = pd.DataFrame({"team_id": teams,
                      "pred_net_rating_dev": [rating_map[t] for t in teams]})
    sim, wins = simulate_season(r, sched, hca=hca, margin_sd=msd, sigma_rating=sigma,
                                n_sims=N_SIMS, seed=seed)
    order = {t: i for i, t in enumerate(sim.team_id)}
    return wins, order


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    imp, pts, pgl, gl, pa, ts, rosters, ages = _load_common()
    print("building preseason prior pipeline...", flush=True)
    cal, actual2 = build_prior_frame(imp, pts, pgl, ts, ages, rosters)

    for N in SPLITS:
        # Precompute per-season state (obs, prior, remaining schedule, params).
        per = {}
        for s in range(2016, LAST + 1):
            gs = gl[gl.SEASON_START == s].copy()
            if gs.empty:
                continue
            state, d = through_split(gs, N)
            rem_sched = extract_schedule(gs[gs.GAME_DATE >= d], s)
            if rem_sched.empty:
                continue
            hca, msd = estimate_game_params(gl, before_season=s)
            sigma = fit_rating_sigma(cal, actual2[["team_id", "season_start", "net_rating_dev"]],
                                     before_season=s)
            per[s] = {"state": state, "prior": prior_for(cal, ts, s),
                      "rem_sched": rem_sched, "hca": hca, "msd": msd, "sigma": sigma}

        rows = []
        for S in range(FIRST, LAST + 1):
            short = S in SHORT or (S - 1) in SHORT
            if short or S not in per:
                continue
            train = [per[s] for s in per if s < S and s not in SHORT and (s - 1) not in SHORT]
            if not train:
                continue
            w = fit_w(train)
            st = per[S]
            teams = list(st["state"].team_id)
            prior = st["prior"]
            banked = dict(zip(st["state"].team_id, st["state"].banked_wins))
            actual_rem = dict(zip(st["state"].team_id, st["state"].rem_wins))
            obs = dict(zip(st["state"].team_id, st["state"].obs_rating))
            rmap = blend_rating(st["state"], prior, w)
            variants = {"model": rmap,
                        "preseason": {t: prior.get(t, 0.0) for t in teams},
                        "naive": {t: obs[t] for t in teams}}
            res = {}
            for name, rm in variants.items():
                wins, order = _sim_wins(rm, st["rem_sched"], st["hca"], st["msd"],
                                        st["sigma"], 1000 + S, teams)
                pred = {t: wins[:, order[t]].mean() for t in teams}
                mae = np.mean([abs(pred[t] - actual_rem[t]) for t in teams])
                # full-season-equivalent (banked + predicted remaining vs actual full wins)
                full_mae = np.mean([abs((banked[t] + pred[t])
                                        - (banked[t] + actual_rem[t])) for t in teams])
                res[name] = {"mae": mae, "full_mae": full_mae}
            rows.append({"season": S, "w": w, **{f"{k}_mae": v["mae"] for k, v in res.items()},
                         **{f"{k}_full": v["full_mae"] for k, v in res.items()}})

        df = pd.DataFrame(rows)
        print(f"\n===== N = {N} games played  ({len(df)} folds, exShort) =====")
        print(df.round(3).to_string(index=False))
        print(f"\n  rest-of-season MAE:  model {df.model_mae.mean():.3f}   "
              f"preseason {df.preseason_mae.mean():.3f}   naive {df.naive_mae.mean():.3f}")
        dp = df.preseason_mae - df.model_mae
        dn = df.naive_mae - df.model_mae
        print(f"  model vs preseason:  {dp.mean():+.3f}  SE {dp.std()/np.sqrt(len(dp)):.3f}  "
              f"{(dp>0).sum()}/{len(dp)} folds better")
        print(f"  model vs naive:      {dn.mean():+.3f}  SE {dn.std()/np.sqrt(len(dn)):.3f}  "
              f"{(dn>0).sum()}/{len(dn)} folds better")
        print(f"  mean fitted w({N}) = {df.w.mean():.2f}  (0=preseason, 1=naive pace)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
