"""NHL Stage 3, step 4: forward-projection validation + face-validity.

Checks that projection.project (aged, mean-regressed talent) is well-calibrated for next season
and shows the projected leaderboard. Calibration = regress actual single-season net in Y on the
projection made from Y-1; a well-calibrated projection has slope ~1 and correlation ~= talent's
(aging is one-year-neutral and the per-skill persistence is a shrink, so correlation tracks talent).

    python scripts/nhl_stage3_projection_report.py

NOTE: the persistence constants live in nhl.projection and were fit on these same folds, so the
calibration slope is in-sample (confirms the mechanics); a walk-forward refit of beta is a
refinement. Legend: off/def/net = xG per 60 (5v5); slope = actual-on-projected regression slope.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import projection  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, nargs="+", default=[2019, 2020, 2021, 2022, 2023, 2024])
    ap.add_argument("--show", type=int, default=2025, help="season end-year to print the projection for")
    args = ap.parse_args()

    print("Forward projection = beta * (talent + aging), per skill "
          f"(off {projection.PERSISTENCE_OFF}, def {projection.PERSISTENCE_DEF}).\n")
    print("== calibration: actual net in Y vs projection from Y-1 ==")
    print(f"{'target':>9} {'slope':>7} {'corr':>7} {'projSD':>7} {'actualSD':>9} {'n':>5}")
    slopes, corrs = [], []
    for Y in args.targets:
        try:
            proj = projection.project(Y - 1)[["player_id", "net"]].rename(columns={"net": "p"})
        except Exception as err:  # noqa: BLE001
            print(f"  (skip {season_str(Y)}: {err})"); continue
        tgt = pd.read_parquet(PROC / f"impact_{Y}_a3000.parquet")[["player_id", "net"]].rename(columns={"net": "a"})
        d = proj.merge(tgt, on="player_id")
        slope = np.polyfit(d["p"], d["a"], 1)[0]
        corr = np.corrcoef(d["p"], d["a"])[0, 1]
        slopes.append(slope); corrs.append(corr)
        print(f"{season_str(Y):>9} {slope:>7.2f} {corr:>7.3f} {d['p'].std():>7.3f} {d['a'].std():>9.3f} {len(d):>5}")
    if slopes:
        print(f"{'mean':>9} {np.mean(slopes):>7.2f} {np.mean(corrs):>7.3f}   "
              f"(slope ~1 = calibrated; corr ~= talent's ~0.37)")

    print(f"\n== projected {season_str(args.show)} -> {season_str(args.show + 1)}: top 12 by net ==")
    proj = projection.project(args.show)
    sk = pd.read_parquet(PROC / "moneypuck_skaters.parquet").query(f"season_start=={args.show} and situation=='all'")
    names = dict(zip(sk["playerId"].astype(int), sk["name"]))
    proj["name"] = proj["player_id"].map(lambda p: names.get(int(p), str(p)))
    show = proj.head(12)[["name", "age", "talent_net", "off", "def", "net"]]
    print(f"{'name':>22} {'age':>4} {'talent':>7} {'projOff':>8} {'projDef':>8} {'projNet':>8}")
    for _, r in show.iterrows():
        print(f"{r['name'][:22]:>22} {r['age']:>4.0f} {r['talent_net']:>+7.2f} "
              f"{r['off']:>+8.2f} {r['def']:>+8.2f} {r['net']:>+8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
