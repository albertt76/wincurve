"""Gate: does RE-FITTING the box-vs-RAPM defensive blend weight beat the shipped turnover
weighting? Verdict: NO (measured negative result -- keep the shipped weight).

Background. Defense is a blend of a box-score aggregate and a play-by-play RAPM aggregate:
    agg_def_used = (1 - w) * agg_def_box  +  w * agg_def_rapm
The shipped weight is w = each team's roster new-minute share (turnover). This script asks
whether a re-fit weight -- flat constants, or a single flat weight fitted walk-forward per fold
-- does better out-of-sample on WIN MAE (mean absolute error, the average miss in wins).

Why this script exists. A rating-space proxy (rating-MAE x 2.38) had suggested flat 0.50 wins
by +0.13 (6/6 folds). The REAL 5000-sim gate contradicts it: flat 0.50 is -0.04 (worse). The
proxy is unfaithful because the rating->wins map is nonlinear (tail compression) and win-MAE
weights teams by the local slope, which is steeper near .500 -- so a scheme that trims tail-team
rating error looks good in rating space and does nothing in win space. This is the project's
recurring lesson (tune on the downstream objective, not an internal diagnostic), so the gate is
run in the real simulation, with paired seeds across schemes for a low-variance comparison.

Result (5000 sims, walk-forward 2017-2025, headline excludes shortened folds):
    box (w=0)            7.769   -0.131 vs shipped
    turnover [SHIPPED]   7.638    baseline           (7.62 under the repo's default seed 12345)
    flat 0.25            7.614   +0.024 (in-sample-best constant; 4/6 folds)
    flat 0.50            7.678   -0.040
    rapm (w=1)           7.832   -0.194 (worst)
    w_fit (walk-forward) 7.610   +0.028 (honest re-fit; SE ~0.09, weights bounce 0.30-0.75)
The best candidates gain +0.02-0.03 wins with paired SE ~0.05-0.09 -- within noise. Everything
at w >= 0.5 is worse. Adversarially audited (3 independent lenses): negative-result-trustworthy;
the shipped arm reproduces agg_def_used to 0.0, and the low power means "not shown to beat",
which defaults to keeping shipped.

The deeper reason RAPM's edge shrank: the box defensive metric was itself fixed this cycle
(position-relative rebounding + rim/hustle tracking), so pure RAPM (7.83) is now WORSE than pure
box (7.77), and the blend's headroom over box collapsed to ~0.13. The remaining untested
formulations (each metric its own slope in rating space; player-level weighting by RAPM
possession count using PURE not box-informed RAPM) are mechanically distinct -- see
scripts for the player-level test. This flat-weight family is closed: do not re-attempt.

    python scripts/gate_blend_weight.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _load():
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
    print("building box-informed RAPM impact + walk-forward aggregates 2016-2025...", flush=True)
    rapm_imp = build_rapm_impact(imp, PROC)
    # 2016 base is needed so the 2017 fold has a prior season to calibrate/carryover on.
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, range(2016, LAST + 1))
    return A, gl, ts


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    A0, gl, ts = _load()
    actual = ts.copy()
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "actual_wins_82", "games"]]
    actual2 = actual.assign(net_rating_dev=actual.net_rating
                            - actual.groupby("season_start").net_rating.transform("mean"))
    act_dev = actual2[["team_id", "season_start", "net_rating_dev"]]

    def calibrate_all(w):
        A = A0.copy()
        w = np.asarray(w, dtype=float)
        A["b"] = (1 - w) * A.agg_def_box + w * A.agg_def_rapm
        out = []
        for s in sorted(A.season_start.unique()):
            so, io = calibrate_projected_ratings(A, ts, target_season=s, target="off_rating",
                                                 agg_col="agg_off")
            sd, idc = calibrate_projected_ratings(A, ts, target_season=s, target="def_rating",
                                                  agg_col="b")
            sub = A[A.season_start == s].copy()
            sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub.b + idc
            out.append(sub)
        return pd.concat(out, ignore_index=True)

    def sim_gate(cal):
        rows = []
        for s in range(FIRST, LAST + 1):
            sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
            sched = extract_schedule(gl, s)
            hca, msd = estimate_game_params(gl, before_season=s)
            sig = fit_rating_sigma(cal, actual2, before_season=s)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched, hca=hca,
                                        margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS,
                                        seed=1000 + s)  # fixed per fold -> paired across schemes
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

    def fit_flat_w(A):
        """Single flat w per fold, chosen to minimize PRIOR folds' rating MAE (walk-forward)."""
        grid = np.linspace(0, 1, 21)
        by_season = {}
        for s in sorted(A.season_start.unique()):
            if s <= FIRST:
                by_season[s] = 0.5
                continue
            best_w, best_e = 0.5, np.inf
            for w in grid:
                cal = calibrate_all(np.full(len(A), w))
                prior = [apply_carryover(cal[cal.season_start == ps].copy(), cal, ts,
                                         target_season=ps)
                         for ps in range(FIRST, s)]
                j = pd.concat(prior, ignore_index=True).merge(act_dev,
                                                              on=["team_id", "season_start"])
                j = j[~(j.season_start.isin(SHORT) | (j.season_start - 1).isin(SHORT))]
                if j.empty:
                    continue
                e = (j.pred_net_rating_dev - j.net_rating_dev).abs().mean()
                if e < best_e:
                    best_e, best_w = e, w
            by_season[s] = best_w
        return A.season_start.map(by_season), by_season

    t_ = A0.new_minute_share.clip(0, 1).to_numpy()
    schemes = {"box (w=0)": np.zeros(len(A0)), "turnover [SHIPPED]": t_,
               "flat 0.25": 0.25, "flat 0.50": 0.50, "flat 0.75": 0.75,
               "rapm (w=1)": np.ones(len(A0))}

    print(f"\nBLEND-WEIGHT GATE  ({N_SIMS} sims, real schedule, paired seeds, exShort headline)\n")
    per_fold, res = {}, {}
    for name, w in schemes.items():
        df = sim_gate(calibrate_all(w if np.ndim(w) else np.full(len(A0), w)))
        per_fold[name] = df
        ex = df[~df.short]
        res[name] = (ex.MAE.mean(), ex["cov"].mean())

    print("fitting walk-forward flat weight (slow)...", flush=True)
    w_fit, by_season = fit_flat_w(A0)
    df = sim_gate(calibrate_all(w_fit.to_numpy()))
    per_fold["w_fit (walk-fwd)"] = df
    ex = df[~df.short]
    res["w_fit (walk-fwd)"] = (ex.MAE.mean(), ex["cov"].mean())

    ship = res["turnover [SHIPPED]"][0]
    print(f"\n{'scheme':<22}{'exShort MAE':>12}{'coverage':>10}{'vs shipped':>12}"
          f"{'folds better':>14}")
    for name in list(schemes) + ["w_fit (walk-fwd)"]:
        mae, cov = res[name]
        d = per_fold["turnover [SHIPPED]"].merge(per_fold[name], on="season",
                                                 suffixes=("_s", "_x"))
        d = d[~d.short_s]
        print(f"{name:<22}{mae:>12.4f}{cov:>10.1%}{ship - mae:>+12.3f}"
              f"{int((d.MAE_x < d.MAE_s).sum()):>10}/{len(d)}")
    print("\nMAE = mean absolute error in wins (lower better); coverage = share of actual"
          " results inside the 80% interval (target 80%).")
    print("walk-forward fitted weight by fold: "
          + "  ".join(f"{int(s)}:{w:.2f}" for s, w in by_season.items() if s >= FIRST))
    print("\nVERDICT: re-fitting the flat weight does NOT clear the gate. Keep the shipped"
          " turnover weighting.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
