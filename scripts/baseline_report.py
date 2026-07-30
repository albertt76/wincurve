"""Stage 1: establish the bar.

Reports the walk-forward accuracy of the naive baselines and the theoretical noise
floor, so every later stage has something honest to be measured against.

    python scripts/baseline_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nbaproj.baselines import noise_floor, score, walk_forward  # noqa: E402
from nbaproj.teams import load_team_seasons, summarize  # noqa: E402

FIRST_TEST_SEASON = 2013


def main() -> int:
    pd.set_option("display.width", 100)

    teams = load_team_seasons()
    print("=" * 68)
    print("TEAM-SEASON TABLE")
    print("=" * 68)
    print(summarize(teams))

    preds = walk_forward(teams, first_test_season=FIRST_TEST_SEASON)
    print()
    print("=" * 68)
    print(f"WALK-FORWARD BASELINES  ({preds['season'].min()} .. "
          f"{preds['season'].max()}, {len(preds)} team-seasons)")
    print("=" * 68)
    print("Each season predicted using only prior seasons.")
    print("Errors in 82-game-equivalent wins.\n")
    print(score(preds).to_string(index=False, float_format=lambda v: f"{v:7.3f}"))

    floor = noise_floor(teams)
    print()
    print("=" * 68)
    print("IRREDUCIBLE NOISE FLOOR")
    print("=" * 68)
    print("A model that knew every team's exact true strength would still miss")
    print("by this much, because an 82-game record is a finite sample from it.\n")
    for key, val in floor.items():
        print(f"  {key:<26} {val:7.3f}")

    k_by_season = preds.groupby("season", observed=True)["k_fitted"].first()
    print()
    print("Fitted reversion coefficient k by test season")
    print("  (1.0 = pure persistence, 0.0 = predict .500 for everyone)")
    print(f"  range {k_by_season.min():.3f} .. {k_by_season.max():.3f}, "
          f"latest {k_by_season.iloc[-1]:.3f}")

    out = Path("data/processed/baseline_predictions.parquet")
    preds.to_parquet(out, index=False)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
