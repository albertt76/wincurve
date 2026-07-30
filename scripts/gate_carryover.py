"""Gate the one-year residual carryover end-to-end, walk-forward, in the shippable mode.

Runs the full pipeline (roster-mode projection -> calibration -> Monte Carlo) for
2017-2025 with and without the carryover, and reports win MAE and interval coverage. The
carryover ships only if it improves win MAE here -- the same discipline every stage has had.

    python scripts/gate_carryover.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.carryover import apply_carryover, fit_rho  # noqa: E402
from nbaproj.project import (  # noqa: E402
    calibrate_projected_ratings, project_team_ratings,
)
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season,
)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons,
)

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
MODE = "roster"
N_SIMS = 4000
SHORT_STARTS = {int(s[:4]) for s in SHORTENED_SEASONS}


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
        "age": pd.to_numeric(pa["AGE"], errors="coerce"),
    }).drop_duplicates()

    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "net_rating_dev", "actual_wins_82", "games"]]

    # --- one walk-forward projection per season, calibrated on prior folds ---
    # Rosters exist from 2016, so the earliest usable prior for a carryover is 2016.
    raw = []
    for season in range(FIRST_TEST - 1, LAST_TEST + 1):
        r = project_team_ratings(imp, pts, pgl, ts, ages, target_season=season,
                                 mode=MODE, team_rosters=rosters)
        if not r.empty:
            raw.append(r)
    raw = pd.concat(raw, ignore_index=True)

    calibrated = []
    for season in sorted(raw["season_start"].unique()):
        sub = raw[raw["season_start"] == season].copy()
        slope, icept = calibrate_projected_ratings(raw, ts, target_season=season)
        sub["pred_net_rating_dev"] = slope * sub["agg_impact"] + icept
        calibrated.append(sub)
    calibrated = pd.concat(calibrated, ignore_index=True)

    def run(use_carryover: bool) -> pd.DataFrame:
        rows = []
        for season in range(FIRST_TEST, LAST_TEST + 1):
            sub = calibrated[calibrated["season_start"] == season].copy()
            if use_carryover:
                sub = apply_carryover(sub, calibrated, ts, target_season=season)
            sched = extract_schedule(gl, season)
            hca, msd = estimate_game_params(gl, before_season=season)
            sigma = fit_rating_sigma(calibrated, ac, before_season=season)
            sim, wins = simulate_season(
                sub[["team_id", "pred_net_rating_dev"]], sched,
                hca=hca, margin_sd=msd, sigma_rating=sigma, n_sims=N_SIMS)
            j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=season)
                 .merge(ac, on=["team_id", "season_start"]))
            assert len(j) <= 30, f"{season}: {len(j)} rows"
            gp = FULL_SEASON_GAMES / j["games"].fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(sim["team_id"])}
            w82 = wins[:, j["team_id"].map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j["actual_wins_82"].to_numpy()
            pred = w82.mean(axis=0)
            rho = (fit_rho(calibrated, ts, before_season=season)
                   if use_carryover else 0.0)
            rows.append({
                "season": season,
                "MAE": float(np.abs(actv - pred).mean()),
                "cov80": float(((actv >= np.percentile(w82, 10, axis=0))
                                & (actv <= np.percentile(w82, 90, axis=0))).mean()),
                "rho": rho,
                "short_prior": (season - 1) in SHORT_STARTS,
            })
        return pd.DataFrame(rows)

    base = run(False)
    carry = run(True)
    merged = base.merge(carry, on="season", suffixes=("_base", "_carry"))
    merged["delta"] = merged["MAE_base"] - merged["MAE_carry"]

    print("=" * 72)
    print(f"ONE-YEAR RESIDUAL CARRYOVER GATE  (mode={MODE}, {N_SIMS} sims/fold)")
    print("=" * 72)
    print("MAE = mean absolute error in wins; delta > 0 means the carryover helped.\n")
    show = merged[["season", "rho_carry", "MAE_base", "MAE_carry", "delta",
                   "cov80_carry", "short_prior_carry"]]
    show = show.rename(columns={"rho_carry": "rho", "cov80_carry": "cov80",
                                "short_prior_carry": "short_prior"})
    print(show.to_string(index=False, float_format=lambda v: f"{v:6.3f}"))

    full = merged
    ex = merged[~merged["short_prior_carry"]]
    print(f"\n  mean MAE  base {full['MAE_base'].mean():.3f}  "
          f"->  carryover {full['MAE_carry'].mean():.3f}  "
          f"(delta {full['delta'].mean():+.3f} wins)")
    print(f"  excl. shortened-prior folds:  base {ex['MAE_base'].mean():.3f}  "
          f"->  carryover {ex['MAE_carry'].mean():.3f}  "
          f"(delta {ex['delta'].mean():+.3f} wins)")
    print(f"  folds improved: {(full['delta'] > 0).sum()}/{len(full)}")
    print(f"  mean 80% coverage: base {full['cov80_base'].mean():.1%}  "
          f"carryover {full['cov80_carry'].mean():.1%}")

    verdict = "SHIP" if ex["delta"].mean() > 0.02 else "DO NOT SHIP"
    print(f"\n  => {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
