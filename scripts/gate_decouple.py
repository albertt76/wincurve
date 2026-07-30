"""Gate the offense/defense decouple end-to-end, walk-forward, in the shippable mode.

The projection can value each player's total impact as one skill, or value his offense and
defense separately (each with its own aging curve) and calibrate each against team offense and
team defense with its own slope. Decoupling is what lets the app attribute a rating -- and a
market disagreement -- to offense vs defense, and surface where our (weak) box-score defense
disagrees with play-by-play RAPM. But it only ships if it does not cost net win accuracy.

Runs the full pipeline (roster mode -> calibration -> Monte Carlo -> carryover) for 2017-2025
coupled vs decoupled and reports win MAE and 80% interval coverage. simulate_season is seeded,
so the two arms are perfectly paired.

    python scripts/gate_decouple.py

Result (2026-07): MAE-neutral (excl. shortened-prior folds 7.960 -> 7.957) with slightly
better coverage (80.0% -> 80.7%). Shipped: it does not cost accuracy and unlocks the split.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.project import calibrate_projected_ratings, project_team_ratings  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season,
)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons,
)

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
N_SIMS = 6000
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
        "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()

    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "net_rating_dev", "actual_wins_82", "games"]]

    def calibrated(decouple: bool) -> pd.DataFrame:
        raw = []
        for season in range(FIRST_TEST - 1, LAST_TEST + 1):
            r = project_team_ratings(imp, pts, pgl, ts, ages, target_season=season,
                                     mode="roster", team_rosters=rosters, decouple=decouple)
            if not r.empty:
                raw.append(r)
        raw = pd.concat(raw, ignore_index=True)
        out = []
        for season in sorted(raw["season_start"].unique()):
            sub = raw[raw["season_start"] == season].copy()
            if decouple:
                so, io = calibrate_projected_ratings(
                    raw, ts, target_season=season, target="off_rating", agg_col="agg_off")
                sd, idc = calibrate_projected_ratings(
                    raw, ts, target_season=season, target="def_rating", agg_col="agg_def")
                sub["pred_net_rating_dev"] = so * sub["agg_off"] + io + sd * sub["agg_def"] + idc
            else:
                sl, ic = calibrate_projected_ratings(raw, ts, target_season=season)
                sub["pred_net_rating_dev"] = sl * sub["agg_impact"] + ic
            out.append(sub)
        return pd.concat(out, ignore_index=True)

    def run(decouple: bool) -> pd.DataFrame:
        cal = calibrated(decouple)
        rows = []
        for season in range(FIRST_TEST, LAST_TEST + 1):
            sub = apply_carryover(cal[cal["season_start"] == season].copy(), cal, ts,
                                  target_season=season)
            sched = extract_schedule(gl, season)
            hca, msd = estimate_game_params(gl, before_season=season)
            sigma = fit_rating_sigma(cal, ac, before_season=season)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched,
                                        hca=hca, margin_sd=msd, sigma_rating=sigma,
                                        n_sims=N_SIMS)
            j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=season)
                 .merge(ac, on=["team_id", "season_start"]))
            gp = FULL_SEASON_GAMES / j["games"].fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(sim["team_id"])}
            w82 = wins[:, j["team_id"].map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j["actual_wins_82"].to_numpy()
            rows.append({"season": season, "MAE": float(np.abs(actv - w82.mean(axis=0)).mean()),
                         "cov80": float(((actv >= np.percentile(w82, 10, axis=0))
                                         & (actv <= np.percentile(w82, 90, axis=0))).mean()),
                         "short": (season - 1) in SHORT_STARTS or season in SHORT_STARTS})
        return pd.DataFrame(rows)

    c, d = run(False), run(True)
    m = c.merge(d, on="season", suffixes=("_c", "_d"))
    m["delta"] = m["MAE_c"] - m["MAE_d"]

    print("=" * 68)
    print(f"OFFENSE/DEFENSE DECOUPLE GATE  ({N_SIMS} sims/fold, roster mode + carryover)")
    print("=" * 68)
    print("MAE = mean absolute error in wins; delta > 0 means decoupling helped.\n")
    print("season short  coupled  decoupled   delta   cov_c  cov_d")
    for _, r in m.iterrows():
        print(f"  {int(r['season'])} {str(bool(r['short_c'])):>5}  {r['MAE_c']:7.3f}  "
              f"{r['MAE_d']:8.3f}  {r['delta']:+6.3f}  {r['cov80_c']:5.0%} {r['cov80_d']:5.0%}")
    ex = m[~m["short_c"]]
    print(f"\n  excl. shortened-prior folds:  coupled {ex['MAE_c'].mean():.3f}  ->  "
          f"decoupled {ex['MAE_d'].mean():.3f}  (delta {ex['delta'].mean():+.3f})")
    print(f"  mean 80% coverage:  coupled {m['cov80_c'].mean():.1%}  "
          f"decoupled {m['cov80_d'].mean():.1%}")
    verdict = "SHIP" if ex["delta"].mean() > -0.05 else "DO NOT SHIP"
    print(f"\n  => {verdict}  (neutral-or-better MAE; decoupling unlocks the off/def split)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
