"""Gate: does re-anchoring the box-informed RAPM prior on the CURRENT box defense beat the stale
prior? Verdict: YES -- shipped. Headline MAE 7.61 -> ~7.57.

The RAPM parquets used to be fit with the box prior as it stood when they were generated -- before
position-relative rebounding, rim/hustle tracking, and the BPM position fallback improved the box
defensive metric -- so the shipped RAPM arm was anchored to a stale box. `scripts/build_rapm.py`
now regenerates them against the current box defense (contemporaneous, point-in-time-safe).

Measured result (5000 sims, walk-forward 2017-2025, paired seeds, exShort headline):
    OLD (stale RAPM prior)    MAE 7.628   coverage 78.3%
    NEW (current-box prior)   MAE 7.591   coverage 78.9%
    delta +0.036, SE 0.017 (~2.2 SE), 5/6 folds improved (biggest 2025-26 +0.098, 2018-19 +0.073).

## Reproducibility note

The NEW side is fully reproducible: `python scripts/build_rapm.py --refresh` regenerates it from
cached stints + the current `player_impact.parquet`. The OLD (stale-prior) side is NOT
reconstructable from committed state -- it used a box `def_impact` from before this cycle's
defensive fixes, which no longer exists. So this script gates the shipped RAPM (`data/processed`)
against a baseline directory the caller supplies (e.g. a backup of the pre-refit parquets taken
before promotion); with no baseline it just reproduces and prints the shipped NEW pipeline MAE as
a verification. The +0.036 figure above is the one-time OLD->NEW measurement, recorded here so it
is not lost.

    python scripts/gate_defense_prior.py                          # verify shipped NEW MAE
    python scripts/gate_defense_prior.py --baseline-dir <backup>  # A/B vs a stale-prior backup
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons)

PROC = Path("data/processed")
FIRST, LAST, N_SIMS = 2017, 2025, 5000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}


def _run(rapm_dir, imp, pts, pgl, gl, ts, rosters, ages, ac, actual2):
    rapm_imp = build_rapm_impact(imp, rapm_dir)
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
    rows = []
    for s in range(FIRST, LAST + 1):
        sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
        sched = extract_schedule(gl, s)
        hca, msd = estimate_game_params(gl, before_season=s)
        sig = fit_rating_sigma(cal, actual2, before_season=s)
        sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched, hca=hca,
                                    margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS, seed=1000 + s)
        j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=s)
             .merge(ac, on=["team_id", "season_start"]))
        gp = FULL_SEASON_GAMES / j.games.fillna(FULL_SEASON_GAMES)
        order = {t: i for i, t in enumerate(sim.team_id)}
        w82 = wins[:, j.team_id.map(order).to_numpy()] * gp.to_numpy()[None, :]
        actv = j.actual_wins_82.to_numpy()
        rows.append({"season": s, "MAE": float(np.abs(actv - w82.mean(0)).mean()),
                     "cov": float(((actv >= np.percentile(w82, 10, 0))
                                   & (actv <= np.percentile(w82, 90, 0))).mean()),
                     "short": (s - 1) in SHORT or s in SHORT})
    return pd.DataFrame(rows)


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-dir", default=None,
                    help="dir of stale-prior rapm_*.parquet to A/B against (optional)")
    args = ap.parse_args()

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ages = pd.DataFrame({"player_id": pa["PLAYER_ID"].astype("int64"),
                         "season_start": pa["SEASON_START"].astype(int),
                         "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()
    actual = ts.copy()
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "actual_wins_82", "games"]]
    actual2 = actual.assign(net_rating_dev=actual.net_rating
                            - actual.groupby("season_start").net_rating.transform("mean"))
    common = (imp, pts, pgl, gl, ts, rosters, ages, ac, actual2)

    new_df = _run(PROC, *common)
    ex = new_df[~new_df.short]
    print(f"NEW (shipped, current-box prior): exShort MAE {ex.MAE.mean():.4f}  "
          f"cov {ex['cov'].mean():.1%}")
    if args.baseline_dir:
        old_df = _run(Path(args.baseline_dir), *common)
        exo = old_df[~old_df.short]
        d = old_df.merge(new_df, on="season", suffixes=("_o", "_n"))
        d = d[~d.short_o]
        diff = d.MAE_o - d.MAE_n
        print(f"BASELINE ({args.baseline_dir}): exShort MAE {exo.MAE.mean():.4f}")
        print(f"delta (baseline-new): {diff.mean():+.4f}  SE {diff.std()/np.sqrt(len(diff)):.4f}  "
              f"{(diff>0).sum()}/{len(diff)} folds improved")
    else:
        print("(no --baseline-dir given; measured OLD->NEW was 7.628 -> 7.591, +0.036, ~2.2 SE)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
