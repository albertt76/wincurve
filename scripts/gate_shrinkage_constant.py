"""Gate: does raising SHRINK_MINUTES (per-skill or globally) beat the shipped 200? Verdict: NO
(measured negative result -- keep 200).

From the 2026-07-31 advanced-metrics deep dive (EPM/DARKO's per-stat shrinkage constants):
`nbaproj.aging.project_next_season` shrinks a player's blended recent record toward the
age-mean with ONE shared `shrink_minutes` (200) for every skill, unlike DARKO/EPM which use
different stabilization constants per input stat. This tests whether off_impact and def_impact
(and the combined "impact") want a different, or just a larger, shrink_minutes now that the
off/def split exists -- the 200 constant was tuned before that split, for the combined skill only.

Stage 1 (cheap, player-level walk-forward projection MAE, `stage2b_report.py`'s own metric):
swept shrink_minutes in {50,100,150,200,300,400,600,900} x 4 recency-weight profiles, separately
for off_impact and def_impact. Result: NOT a per-feature story -- off_impact, def_impact, AND the
combined impact all monotonically improve up to shrink=600 (U-shaped, ticks back up at 900), with
recency weights staying at the current (5,3,2) for all three. This says the SHARED constant is
stale post-decouple, not that skills need DIFFERENT treatment:
    off_impact  200->0.7120  600->0.7017  (-1.4%)
    def_impact  200->0.5225  600->0.5143  (-1.6%)
    impact      200->0.8962  600->0.8834  (-1.4%)

Stage 2 (this script, real 5000-sim gate): the player-level gain does NOT survive team
aggregation -- classic "improves the player metric, dies at the team win number" pattern (same
shape as the RAPM finding). shrink=600 vs shipped 200, walk-forward 2017-2025, exShort:
    shrink=200 [SHIPPED]   MAE 7.6275   coverage 78.3%
    shrink=600             MAE 7.6441   coverage 78.9%
    delta (200-600): -0.017, SE 0.025, only 2/6 folds improved
Not even close to a real signal in EITHER direction -- keep 200. Fold pattern is inconsistent
(2017/2018/2022 worse, 2023/2025 better, no coherent story), unlike the position-estimator fix's
gate, which concentrated its gain exactly where the mechanism predicted.

One harmless, unrelated fix kept from this investigation:
`project_next_season`'s `shrink_minutes` parameter used to default to the SHRINK_MINUTES module
constant bound at function-DEFINITION time (a Python foot-gun -- mutating the module constant
after import silently would not affect already-compiled default arguments). It now defaults to
None and resolves SHRINK_MINUTES at call time instead. This changes nothing when the constant
stays at 200 (verified: reproduces the exact same MAE), but is more correct and is what let this
gate actually test the alternative value. Kept regardless of this negative result.

    python scripts/gate_shrinkage_constant.py
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

import nbaproj.aging as aging_mod  # noqa: E402
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


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
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

    def gate(shrink_minutes):
        aging_mod.SHRINK_MINUTES = shrink_minutes
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
        rows = []
        for s in range(FIRST, LAST + 1):
            sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
            sched = extract_schedule(gl, s)
            hca, msd = estimate_game_params(gl, before_season=s)
            sig = fit_rating_sigma(cal, actual2, before_season=s)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched, hca=hca,
                                        margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS,
                                        seed=1000 + s)
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

    print(f"SHRINK_MINUTES GATE ({N_SIMS} sims, paired seeds, exShort headline)\n")
    old_df = gate(200.0)
    new_df = gate(600.0)
    aging_mod.SHRINK_MINUTES = 200.0  # restore the shipped default
    for lbl, df in [("shrink=200 [SHIPPED]", old_df), ("shrink=600", new_df)]:
        ex = df[~df.short]
        print(f"{lbl:<24} MAE {ex.MAE.mean():.4f}  cov {ex['cov'].mean():.1%}")
    d = old_df.merge(new_df, on="season", suffixes=("_o", "_n"))
    d = d[~d.short_o]
    diff = d.MAE_o - d.MAE_n
    print(f"\ndelta (200-600): {diff.mean():+.4f}  SE {diff.std() / np.sqrt(len(diff)):.4f}  "
          f"{(diff > 0).sum()}/{len(diff)} folds improved")
    print("\nVERDICT: shrink=600 does not beat the shipped shrink=200. Keep 200.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
