"""NHL Stage 4->5: a FIRST end-to-end team POINTS projection, and does it beat the bar?

Pipeline: project(Y-1) skater impacts -> minute-weighted team 5v5 net (nhl.aggregate) -> a
walk-forward linear calibration to 82-game standings points -> per-team projected points. We score
it against the Stage 1 bar (mean-reverted previous points) on the SAME folds.

    python scripts/nhl_stage45_points_report.py

CAVEATS (this is a first cut, expect it to trail a full model):
- Uses each season's ACTUAL roster + TOI to aggregate (roster/minutes are not yet projected) -- a
  LEAKY upper bound, like the NBA project's leaky bound. The honest version needs opening-day roster
  reconstruction + a minutes model (Stage 3b).
- 5v5 skaters ONLY -- no special teams (PP/PK) and no goalie differentiation yet (goalie GSAx is
  near-unpredictable, so it barely moves teams; see goalies.project_gsax).
- Direct net->points linear calibration (no explicit goals / Pythagorean / season simulation yet).
Legend: MAE = mean absolute error in 82-game points; pts82 = point% x 164.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import aggregate, projection  # noqa: E402
from nhl.ingest import PROC, season_id, season_str  # noqa: E402

REF = pd.read_parquet(PROC / "team_reference.parquet")[["team_id", "tricode"]]


def team_points_82(Y: int) -> pd.DataFrame:
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)].merge(REF, left_on="teamId", right_on="team_id")
    ts["pts82"] = ts["points"] / (2 * ts["gamesPlayed"]) * 164.0
    return ts[["tricode", "pts82"]].rename(columns={"tricode": "team"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, nargs="+", default=[2023, 2024, 2025])
    args = ap.parse_args()

    # panel: aggregated projected 5v5 net + actual 82-game points, per team-season (2021-2025)
    panel = []
    for Y in range(2021, 2026):
        g = aggregate.team_ratings(projection.project(Y - 1), Y)[["team", "net"]]
        panel.append(g.merge(team_points_82(Y), on="team").assign(Y=Y))
    P = pd.concat(panel, ignore_index=True)
    gmean = P["pts82"].mean()

    print("First end-to-end NHL points projection (LEAKY roster; 5v5 only; linear calibration).\n")
    print(f"{'season':>9} {'model MAE':>10} {'naive MAE':>10} {'n':>4}   winner")
    mms, nns = [], []
    for Y in args.folds:
        tr, te = P[P["Y"] < Y], P[P["Y"] == Y]
        # model: walk-forward linear calibration net -> pts82
        b1, b0 = np.polyfit(tr["net"], tr["pts82"], 1)
        model_mae = float((b0 + b1 * te["net"] - te["pts82"]).abs().mean())
        # bar: mean-reverted previous points, reversion k fit on prior transitions
        prev = team_points_82(Y - 1).rename(columns={"pts82": "prev"})
        tr_k = P[P["Y"] < Y].merge(
            pd.concat([team_points_82(y - 1).rename(columns={"pts82": "prev"}).assign(Y=y)
                       for y in range(2022, Y)]), on=["team", "Y"])
        k = np.polyfit(tr_k["prev"] - gmean, tr_k["pts82"] - gmean, 1)[0] if len(tr_k) > 10 else 0.5
        naive = te.merge(prev, on="team")
        naive_mae = float((gmean + k * (naive["prev"] - gmean) - naive["pts82"]).abs().mean())
        mms.append(model_mae); nns.append(naive_mae)
        win = "model" if model_mae < naive_mae else "naive"
        print(f"{season_str(Y):>9} {model_mae:>10.2f} {naive_mae:>10.2f} {len(te):>4}   {win}")
    print(f"{'mean':>9} {np.mean(mms):>10.2f} {np.mean(nns):>10.2f}   "
          f"(Stage 1 bar over 2010-2025 = 10.54)")

    # face validity: most recent completed season, projected vs actual
    Y = 2025
    tr = P[P["Y"] < Y]
    b1, b0 = np.polyfit(tr["net"], tr["pts82"], 1)
    te = P[P["Y"] == Y].copy()
    te["proj"] = b0 + b1 * te["net"]
    te = te.sort_values("proj", ascending=False)
    print(f"\n== {season_str(Y)} projected vs actual 82-game points (top/bottom 5) ==")
    print(f"{'team':>5} {'proj':>6} {'actual':>7} {'err':>6}")
    for _, r in pd.concat([te.head(5), te.tail(5)]).iterrows():
        print(f"{r['team']:>5} {r['proj']:>6.0f} {r['pts82']:>7.0f} {r['proj']-r['pts82']:>+6.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
