"""NHL Stage 4: team-aggregation validation.

Does minute-weighted aggregated skater impact reconstruct team even-strength rate? Two checks:
1. CONTEMPORANEOUS -- aggregate single-season RAPM(Y) by team-Y TOI vs team-Y 5v5 xG rate. Validates
   the aggregation mechanism (should be high).
2. PROJECTED (forward) -- aggregate projection.project(Y-1) onto team-Y's actual roster/TOI vs
   team-Y 5v5 xG rate. The real forward test (roster + projection uncertainty).

    python scripts/nhl_stage4_aggregate_report.py

Legend: off/def = xG per 60 (5v5); r = correlation across the 32 teams; cover = share of team 5v5
minutes with an impact estimate. Def correlates with the NEGATIVE of xG-against (more suppression).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import aggregate, projection  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402


def _corrs(g: pd.DataFrame, Y: int) -> tuple[float, float, float]:
    t = aggregate.team_xg_rate(Y)
    m = g.merge(t, on="team")
    return (float(np.corrcoef(m["off"], m["xgf"])[0, 1]),
            float(np.corrcoef(m["def"], -m["xga"])[0, 1]),
            float(np.corrcoef(m["net"], m["xgd"])[0, 1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    args = ap.parse_args()

    print("Team aggregation vs actual team 5v5 xG rate (r across 32 teams).\n")
    print("== CONTEMPORANEOUS: aggregated single-season RAPM(Y) ==")
    print(f"{'season':>9} {'off/xGF':>8} {'def/-xGA':>9} {'net/xGdiff':>11}")
    for Y in args.targets:
        imp = pd.read_parquet(PROC / f"impact_{Y}_a3000.parquet")[["player_id", "off", "def"]]
        ro, rd, rn = _corrs(aggregate.team_ratings(imp, Y), Y)
        print(f"{season_str(Y):>9} {ro:>8.3f} {rd:>9.3f} {rn:>11.3f}")

    print("\n== PROJECTED (forward): project(Y-1) onto team-Y roster ==")
    print(f"{'season':>9} {'off/xGF':>8} {'def/-xGA':>9} {'net/xGdiff':>11} {'cover':>6}")
    for Y in args.targets:
        proj = projection.project(Y - 1)[["player_id", "off", "def"]]
        g = aggregate.team_ratings(proj, Y)
        ro, rd, rn = _corrs(g, Y)
        print(f"{season_str(Y):>9} {ro:>8.3f} {rd:>9.3f} {rn:>11.3f} {g['cover'].mean():>6.0%}")
    print("\nNext Stage 4/5: replacement level for uncovered minutes, goalie GSAx + special teams,\n"
          "impact->goals calibration on projected aggregates, one-year carryover, season simulation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
