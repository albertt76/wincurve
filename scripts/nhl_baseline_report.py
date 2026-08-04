"""NHL Stage 1: establish the bar.

Prints the verified data inventory, then the walk-forward accuracy of the naive
baselines -- the yardstick every later stage must clear. Nothing ships until it beats
**mean-reverted previous points**. Errors are in 82-game-equivalent points.

    python scripts/nhl_baseline_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nhl.baselines import noise_floor, score, walk_forward  # noqa: E402
from nhl.ingest import PROC  # noqa: E402
from nhl.teams import SHORTENED_SEASONS, add_prior_season, target_table  # noqa: E402

LEGEND = """\
Legend (acronyms expanded on first use):
  MAE   mean absolute error -- the average miss in points, ignoring direction
  RMSE  root mean squared error -- like MAE but punishes big misses harder
  pt%   point percentage -- points / (2 * games played); the [0,1] rate we model
  OTL   overtime loss -- a loss past regulation, worth 1 standings point ("loser point")
  xG    expected goals -- the probability a shot becomes a goal, summed (MoneyPuck)
  82-game points -- pt% * 164; a full season's points if you win all 82 in regulation
"""

INVENTORY = [
    ("team_reference.parquet", "franchises (stable franchise_id join key)", None),
    ("season_index.parquet", "per-season rule flags (loser point, ties, dates)", "season_start"),
    ("team_summary.parquet", "team-season records: points, W/L/OTL, goals, PP/PK", "season_start"),
    ("moneypuck_teams.parquet", "team xG by situation (EV / PP / PK)", "season_start"),
    ("moneypuck_skaters.parquet", "skater xG by situation", "season_start"),
    ("moneypuck_goalies.parquet", "goalie xG / goals saved above expected inputs", "season_start"),
]


def inventory() -> None:
    print("=== Data inventory (data/nhl/processed) ===")
    print(f"{'dataset':28s} {'rows':>8s}  {'seasons':>7s}  span")
    for fname, desc, scol in INVENTORY:
        path = PROC / fname
        if not path.exists():
            print(f"{fname:28s} {'MISSING':>8s}")
            continue
        df = pd.read_parquet(path)
        if scol and scol in df.columns:
            yrs = sorted(df[scol].unique())
            span = f"{yrs[0]}-{str(yrs[0] + 1)[-2:]} .. {yrs[-1]}-{str(yrs[-1] + 1)[-2:]}"
            n = len(yrs)
        else:
            span, n = "-", "-"
        print(f"{fname:28s} {len(df):>8,}  {str(n):>7s}  {span}")
        print(f"{'  -> ' + desc:28s}")
    print()


def main() -> int:
    print(LEGEND)
    inventory()

    df = target_table()
    preds = walk_forward(df, first_test_season=2010)
    tbl = score(preds)

    test_seasons = sorted(preds["season_start"].unique())
    print(f"=== Walk-forward baselines, test seasons {test_seasons[0]}-{str(test_seasons[0]+1)[-2:]}"
          f" .. {test_seasons[-1]}-{str(test_seasons[-1]+1)[-2:]}"
          f" ({len(test_seasons)} seasons, {len(preds)} team-seasons) ===")
    print(tbl.to_string(index=False, float_format=lambda x: f"{x:6.2f}"))

    nf = noise_floor(df)
    print("\n=== Context ===")
    print(f"  league-average pt% (loser-point inflated, not 0.500): "
          f"{df['point_pct'].mean():.3f}  ->  {df['point_pct'].mean()*164:.1f} of 164 pts")
    print(f"  fitted reversion k (latest fold): {preds['k_fitted'].iloc[-1]:.3f}  "
          f"(1=persistence, 0=all league-average)")
    print(f"  observed 82-game points SD: {nf['observed_points_82_SD']:.1f}")
    print(f"  approx binomial noise floor (UPPER bound, ignores loser point): "
          f"MAE {nf['approx_binomial_MAE_points']:.1f} pts")
    short = ", ".join(f"{y}-{str(y+1)[-2:]} ({g}g)" for y, g in SHORTENED_SEASONS.items())
    print(f"\n  note: shortened seasons handled by modeling pt% (a rate): {short}")
    print("  note: market (season points over/under) comparison is added in a later stage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
