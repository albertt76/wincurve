"""Stage 4+5: end-to-end walk-forward win projection, measured against the market.

This is the first point the whole pipeline produces the thing we set out to build, so
it is also the first honest test. Two roster variants are reported: the realistic one
and a deliberately leaky upper bound (see nbaproj/project.py).

    python scripts/stage45_report.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.baselines import (  # noqa: E402
    MARKET_SUSPECT_SEASONS, market_wins_82,
)
from nbaproj.odds import load_market_baseline  # noqa: E402
from nbaproj.project import (  # noqa: E402
    calibrate_projected_ratings, project_team_ratings,
)
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season,
)
from nbaproj.teams import FULL_SEASON_GAMES, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
N_SIMS = 3000


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
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
    act_cols = actual[["team_id", "season_start", "net_rating_dev",
                       "actual_wins_82", "games"]]

    market = load_market_baseline(ts)
    market = market[market["wins_ou"].notna()].copy()
    market["market_wins_82"] = market_wins_82(market)
    mkt = market[["team_id", "season_start", "market_wins_82"]]

    print("=" * 74)
    print("STAGE 4+5: END-TO-END WIN PROJECTION")
    print("=" * 74)
    print("Walk-forward: each season projected using only earlier seasons.")
    print("MAE = mean absolute error (average miss, in 82-game-equivalent wins).")
    print("Lower is better.\n")

    all_preds = {"early": [], "actual": []}
    for mode in ("early", "actual"):
        preds = []
        for season in range(FIRST_TEST, LAST_TEST + 1):
            r = project_team_ratings(imp, pts, pgl, ts, ages,
                                     target_season=season, mode=mode)
            if not r.empty:
                preds.append(r)
        if preds:
            all_preds[mode] = pd.concat(preds, ignore_index=True)

    rows = []
    for mode, preds in all_preds.items():
        if len(preds) == 0:
            continue
        for season in range(FIRST_TEST, LAST_TEST + 1):
            sub = preds[preds["season_start"] == season]
            if sub.empty:
                continue
            sched = extract_schedule(gl, season)
            if sched.empty:
                continue
            hca, msd = estimate_game_params(gl, before_season=season)

            # Calibrate on projected aggregates from earlier folds only.
            slope, icept = calibrate_projected_ratings(
                preds, ts, target_season=season)
            sub = sub.copy()
            sub["pred_net_rating_dev"] = slope * sub["agg_impact"] + icept

            scored_all = preds.copy()
            scored_all["pred_net_rating_dev"] = (
                slope * scored_all["agg_impact"] + icept)
            sigma = fit_rating_sigma(scored_all, act_cols, before_season=season)

            sim, wins = simulate_season(
                sub[["team_id", "pred_net_rating_dev"]], sched,
                hca=hca, margin_sd=msd, sigma_rating=sigma, n_sims=N_SIMS)

            # Both merges MUST key on (team_id, season_start). Joining on team_id
            # alone silently produces a cartesian product across all 21 seasons,
            # which inflated row counts to 630 per season and made the market
            # baseline read 11.7 instead of its known 6.67.
            sub_s = sub[["team_id", "pred_net_rating_dev"]].assign(
                season_start=season)
            j = (sim.drop(columns=["pred_net_rating_dev"])
                 .assign(season_start=season)
                 .merge(sub_s, on=["team_id", "season_start"])
                 .merge(act_cols, on=["team_id", "season_start"])
                 .merge(mkt, on=["team_id", "season_start"], how="left"))
            assert len(j) <= 30, f"{season}: {len(j)} rows, expected <=30"
            # Rescale simulated wins to 82-game equivalent for shortened seasons.
            gp = j["games"].fillna(FULL_SEASON_GAMES)
            j["pred_wins_82"] = j["mean_wins"] * FULL_SEASON_GAMES / gp
            rows.append({
                "mode": mode, "season": season, "n": len(j),
                "sigma_rating": sigma, "hca": hca, "margin_sd": msd,
                "slope": slope, "pred_sd": float(sub["pred_net_rating_dev"].std()),
                "MAE_model": (j["actual_wins_82"] - j["pred_wins_82"]).abs().mean(),
                "MAE_market": (j["actual_wins_82"]
                               - j["market_wins_82"]).abs().mean(),
                "rating_MAE": (j["net_rating_dev"]
                               - j["pred_net_rating_dev"]).abs().mean(),
            })

    res = pd.DataFrame(rows)
    for mode in ("early", "actual"):
        sub = res[res["mode"] == mode]
        if sub.empty:
            continue
        label = ("REALISTIC (opening-rotation roster, projected minutes)"
                 if mode == "early" else
                 "LEAKY UPPER BOUND (actual roster + actual minutes)")
        print(f"\n{'-' * 74}\n{label}\n{'-' * 74}")
        print(sub[["season", "n", "MAE_model", "MAE_market", "rating_MAE",
                   "slope", "pred_sd", "sigma_rating"]].to_string(
            index=False, float_format=lambda v: f"{v:7.3f}"))
        clean = sub[~sub["season"].astype(str).isin(
            {s[:4] for s in MARKET_SUSPECT_SEASONS})]
        print(f"\n  mean MAE model  {sub['MAE_model'].mean():6.3f}"
              f"   (excl. 2019-20: {clean['MAE_model'].mean():6.3f})")
        print(f"  mean MAE market {sub['MAE_market'].mean():6.3f}"
              f"   (excl. 2019-20: {clean['MAE_market'].mean():6.3f})")
        diff = sub["MAE_model"].mean() - sub["MAE_market"].mean()
        verdict = "BEATS market" if diff < 0 else "loses to market"
        print(f"  => {verdict} by {abs(diff):.3f} wins of MAE")

    res.to_parquet(PROC / "stage45_results.parquet", index=False)
    print(f"\nwrote {PROC / 'stage45_results.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
