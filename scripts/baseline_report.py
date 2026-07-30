"""Stage 1: establish the bar.

Reports walk-forward accuracy of the naive baselines AND the market (preseason
win totals), plus the theoretical noise floor, so every later stage has something
honest to be measured against.

    python scripts/baseline_report.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.baselines import (  # noqa: E402
    MARKET_SUSPECT_SEASONS,
    market_wins_82,
    noise_floor,
    score,
    walk_forward,
)
from nbaproj.odds import load_market_baseline  # noqa: E402
from nbaproj.teams import FULL_SEASON_GAMES, load_team_seasons, summarize  # noqa: E402

FIRST_TEST_SEASON = 2013


def _metrics(actual: np.ndarray, pred: np.ndarray) -> tuple[float, float]:
    err = actual - pred
    return float(np.abs(err).mean()), float(np.sqrt((err**2).mean()))


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    pd.set_option("display.width", 100)

    teams = load_team_seasons()
    print("=" * 70)
    print("TEAM-SEASON TABLE")
    print("=" * 70)
    print(summarize(teams))

    # --- naive baselines ---
    preds = walk_forward(teams, first_test_season=FIRST_TEST_SEASON)
    print()
    print("=" * 70)
    print(f"WALK-FORWARD BASELINES  ({preds['season'].min()} .. "
          f"{preds['season'].max()}, {len(preds)} team-seasons)")
    print("=" * 70)
    print("MAE  = mean absolute error: average miss, ignoring direction.")
    print("RMSE = root mean squared error: like MAE but penalises big misses more.")
    print("Both in 82-game-equivalent wins. Lower is better.\n")
    naive = score(preds)

    # --- market baseline ---
    market = load_market_baseline(teams)
    market = market[market["wins_ou"].notna()].copy()
    market["market_wins_82"] = market_wins_82(market)
    market["actual_wins_82"] = market["win_pct"] * FULL_SEASON_GAMES

    same_window = market[market["season_start"] >= FIRST_TEST_SEASON]
    clean = same_window[~same_window["season"].isin(MARKET_SUSPECT_SEASONS)]

    rows = naive.to_dict("records")
    for label, subset in [
        ("market (preseason win total)", same_window),
        ("market, excl. 2019-20", clean),
    ]:
        mae, rmse = _metrics(subset["actual_wins_82"].to_numpy(),
                             subset["market_wins_82"].to_numpy())
        rows.append({"baseline": label, "MAE_wins": mae, "RMSE_wins": rmse})

    table = pd.DataFrame(rows).sort_values("MAE_wins")
    print(table.to_string(index=False, float_format=lambda v: f"{v:7.3f}"))

    # --- noise floor ---
    floor = noise_floor(teams)
    print()
    print("=" * 70)
    print("IRREDUCIBLE NOISE FLOOR")
    print("=" * 70)
    print("A model that knew every team's exact true strength would STILL miss by")
    print("this much, because an 82-game record is a finite sample from it.\n")
    print(f"  {'binomial MAE (wins)':<30} {floor['binomial_MAE_wins']:6.2f}")
    print(f"  {'binomial RMSE (wins)':<30} {floor['binomial_RMSE_wins']:6.2f}")
    print(f"  {'observed spread in wins (SD)':<30} "
          f"{floor['observed_wins_82_SD']:6.2f}")
    print("\n  SD = standard deviation: typical distance from the average.")

    talent_sd = np.sqrt(max(
        floor["observed_wins_82_SD"] ** 2 - floor["binomial_RMSE_wins"] ** 2, 0.0))
    print(f"  => implied true-talent spread (SD) {talent_sd:6.2f} wins")
    print("     Talent variation dwarfs luck, so projection is worth doing.")

    # --- headroom ---
    best_naive = naive["MAE_wins"].min()
    mkt_mae = table.loc[table["baseline"] == "market, excl. 2019-20",
                        "MAE_wins"].iloc[0]
    print()
    print("=" * 70)
    print("HEADROOM")
    print("=" * 70)
    print(f"  best naive baseline   {best_naive:6.2f} MAE")
    print(f"  market                {mkt_mae:6.2f} MAE")
    print(f"  noise floor           {floor['binomial_MAE_wins']:6.2f} MAE")
    total = best_naive - floor["binomial_MAE_wins"]
    got = best_naive - mkt_mae
    print(f"\n  naive -> floor gap    {total:6.2f} wins of available improvement")
    print(f"  market captured       {got:6.2f} wins  ({got / total:.0%} of the gap)")
    print(f"  left on the table     {total - got:6.2f} wins  "
          f"({1 - got / total:.0%}) -- our target zone")

    out = Path("data/processed/baseline_predictions.parquet")
    preds.to_parquet(out, index=False)
    market[["season", "team", "team_id", "wins_ou", "market_wins_82",
            "actual_wins_82"]].to_parquet(
        "data/processed/market_baseline.parquet", index=False)
    print(f"\nwrote {out} and data/processed/market_baseline.parquet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
