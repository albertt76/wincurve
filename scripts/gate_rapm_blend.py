"""Gate the RAPM defensive blend end-to-end, walk-forward, in the shippable config.

Blends the box and RAPM defensive team aggregates by roster turnover
(``agg_def = (1-w)*box + w*rapm``, ``w`` = new-minute share), calibrates the defensive slope on
the blend, and runs the full pipeline (roster mode -> calibration -> Monte Carlo -> carryover)
for 2017-2025. Compares win MAE and 80% coverage against the box-only decoupled baseline, and
sweeps the weight so the plateau is visible. simulate_season is seeded, so the arms are paired.

    python scripts/gate_rapm_blend.py

Result (2026-07, RAPM 2013-2025): the turnover-weighted blend is **7.96 -> 7.77 excl. short
folds, all 9 folds improved**, with equal-or-better coverage -- and it lives in the one
decoupled pipeline. Pure box is the baseline; pure RAPM already helps (decoupling was the
unlock); the blend beats both. Shipped.
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
from nbaproj.rapm_blend import backtest_aggregates, blend_weight  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season,
)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons,
)

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
N_SIMS = 5000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ts = load_team_seasons()
    ages = pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()

    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "net_rating_dev", "actual_wins_82", "games"]]

    rapm_imp = build_rapm_impact(imp, PROC)
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters,
                            range(FIRST_TEST - 1, LAST_TEST + 1))

    def run(weight) -> pd.DataFrame:
        """weight: 'box', 'rapm', a float, or 'turnover' -> defensive aggregate to calibrate."""
        B = A.copy()
        if weight == "box":
            B["agg_def_use"] = B["agg_def_box"]
        elif weight == "rapm":
            B["agg_def_use"] = B["agg_def_rapm"]
        elif weight == "turnover":
            B["agg_def_use"] = B["agg_def_used"]
        else:
            w = float(weight)
            B["agg_def_use"] = (1 - w) * B["agg_def_box"] + w * B["agg_def_rapm"]
        cal = []
        for s in sorted(B["season_start"].unique()):
            so, io = calibrate_projected_ratings(B, ts, target_season=s,
                                                 target="off_rating", agg_col="agg_off")
            sd, idc = calibrate_projected_ratings(B, ts, target_season=s,
                                                  target="def_rating", agg_col="agg_def_use")
            sub = B[B["season_start"] == s].copy()
            sub["pred_net_rating_dev"] = so * sub["agg_off"] + io + sd * sub["agg_def_use"] + idc
            cal.append(sub)
        cal = pd.concat(cal, ignore_index=True)
        rows = []
        for s in range(FIRST_TEST, LAST_TEST + 1):
            sub = apply_carryover(cal[cal["season_start"] == s].copy(), cal, ts, target_season=s)
            sched = extract_schedule(gl, s)
            hca, msd = estimate_game_params(gl, before_season=s)
            sig = fit_rating_sigma(cal, ac, before_season=s)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched,
                                        hca=hca, margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS)
            j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=s)
                 .merge(ac, on=["team_id", "season_start"]))
            gp = FULL_SEASON_GAMES / j["games"].fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(sim["team_id"])}
            w82 = wins[:, j["team_id"].map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j["actual_wins_82"].to_numpy()
            rows.append({"season": s, "MAE": float(np.abs(actv - w82.mean(0)).mean()),
                         "cov": float(((actv >= np.percentile(w82, 10, 0))
                                       & (actv <= np.percentile(w82, 90, 0))).mean()),
                         "short": (s - 1) in SHORT or s in SHORT})
        return pd.DataFrame(rows)

    print("=" * 70)
    print(f"RAPM DEFENSIVE BLEND GATE  ({N_SIMS} sims/fold, roster mode + carryover)")
    print("=" * 70)
    print("MAE = mean absolute error in wins; delta = improvement over box-only.\n")
    base = run("box")
    base_ex = base[~base["short"]]["MAE"].mean()
    print(f"{'defensive aggregate':<28} {'MAE_all':>8} {'exShort':>8} {'delta':>7} {'cov':>6} folds+")
    for label, weight in [("box only (baseline)", "box"), ("blend w=0.25", 0.25),
                          ("blend w=0.50", 0.5), ("blend w=turnover", "turnover"),
                          ("pure RAPM def (w=1)", "rapm")]:
        df = run(weight)
        ex = df[~df["short"]]
        d = base.merge(df, on="season", suffixes=("_b", "_x"))
        improved = (d["MAE_b"] > d["MAE_x"]).sum()
        print(f"{label:<28} {df['MAE'].mean():8.3f} {ex['MAE'].mean():8.3f} "
              f"{base_ex - ex['MAE'].mean():+7.3f} {df['cov'].mean():6.0%} {improved}/{len(df)}")

    best = run("turnover")
    m = base.merge(best, on="season", suffixes=("_box", "_bl"))
    m["delta"] = m["MAE_box"] - m["MAE_bl"]
    ex = m[~m["short_box"]]
    se = ex["delta"].std() / np.sqrt(len(ex))
    print(f"\n  turnover blend, excl-short: {base_ex:.3f} -> {ex['MAE_bl'].mean():.3f}  "
          f"(delta {ex['delta'].mean():+.3f} +/- {se:.3f} SE, {(m['delta'] > 0).sum()}/{len(m)} folds)")
    verdict = "SHIP" if ex["delta"].mean() > 0.05 else "borderline / do not ship"
    print(f"  => {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
