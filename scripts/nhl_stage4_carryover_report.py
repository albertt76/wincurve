"""NHL Stage 4: one-year carryover on the team points projection (the NBA's biggest lever).

The roster-based projection misses persistent team-specific effects (coaching/system/goalie luck
that repeats). We add ``rho * last-season residual`` to this season's projection, ``rho`` fit
walk-forward, exactly as the NBA project does. Residual persistence measured at AR(1) rho~0.37
(corr 0.38) -- essentially the NBA's ~0.36. This scores plain vs +carryover vs the naive bar.

    python scripts/nhl_stage4_carryover_report.py

CAVEATS unchanged from the first end-to-end (leaky roster, 5v5-only, linear net->points); the
carryover is orthogonal to those. Legend: MAE in 82-game points; resid = actual - plain projection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import aggregate, projection  # noqa: E402
from nhl.ingest import PROC, season_id, season_str  # noqa: E402

REF = pd.read_parquet(PROC / "team_reference.parquet")[["team_id", "tricode"]]


def pts82(Y: int) -> pd.DataFrame:
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)].merge(REF, left_on="teamId", right_on="team_id")
    ts["pts82"] = ts["points"] / (2 * ts["gamesPlayed"]) * 164.0
    return ts[["tricode", "pts82"]].rename(columns={"tricode": "team"})


def main() -> int:
    P = []
    for Y in range(2019, 2026):
        g = aggregate.team_ratings(projection.project(Y - 1), Y)[["team", "net"]]
        P.append(g.merge(pts82(Y), on="team").assign(Y=Y))
    P = pd.concat(P, ignore_index=True)
    gmean = P["pts82"].mean()

    # walk-forward plain prediction per fold (calibration fit on strictly-earlier folds)
    P["pred"] = np.nan
    for Y in sorted(P["Y"].unique()):
        tr = P[P["Y"] < Y]
        if len(tr) < 10:
            continue
        b1, b0 = np.polyfit(tr["net"], tr["pts82"], 1)
        P.loc[P["Y"] == Y, "pred"] = b0 + b1 * P.loc[P["Y"] == Y, "net"]
    P["resid"] = P["pts82"] - P["pred"]

    print("One-year carryover on the NHL points projection (walk-forward).\n")
    print(f"{'season':>9} {'plain':>7} {'+carry':>7} {'naive':>7} {'rho':>5} {'n':>4}")
    plains, carries, naives = [], [], []
    for Y in [2023, 2024, 2025]:
        te = P[P["Y"] == Y].dropna(subset=["pred"]).copy()
        # last-season residual per team, and rho fit on residual pairs strictly before Y
        prev = P[P["Y"] == Y - 1][["team", "resid"]].rename(columns={"resid": "resid_prev"})
        pr = P[(P["Y"] < Y)].merge(
            P.assign(Y=P["Y"] + 1)[["team", "Y", "resid"]].rename(columns={"resid": "rp"}),
            on=["team", "Y"]).dropna(subset=["resid", "rp"])
        rho = float(np.polyfit(pr["rp"], pr["resid"], 1)[0]) if len(pr) > 10 else 0.0
        te = te.merge(prev, on="team", how="left").fillna({"resid_prev": 0.0})
        te["carry"] = te["pred"] + rho * te["resid_prev"]
        # naive bar: mean-reverted previous points (k on prior transitions)
        pv = pts82(Y - 1).rename(columns={"pts82": "prev"})
        prk = pd.concat([pts82(y - 1).rename(columns={"pts82": "prev"}).assign(Y=y)
                         for y in range(2020, Y)])
        trk = P[P["Y"] < Y].merge(prk, on=["team", "Y"])
        k = float(np.polyfit(trk["prev"] - gmean, trk["pts82"] - gmean, 1)[0]) if len(trk) > 10 else 0.5
        nv = te.merge(pv, on="team")
        mp = float((te["pred"] - te["pts82"]).abs().mean())
        mc = float((te["carry"] - te["pts82"]).abs().mean())
        mn = float((gmean + k * (nv["prev"] - gmean) - nv["pts82"]).abs().mean())
        plains.append(mp); carries.append(mc); naives.append(mn)
        print(f"{season_str(Y):>9} {mp:>7.2f} {mc:>7.2f} {mn:>7.2f} {rho:>5.2f} {len(te):>4}")
    print(f"{'mean':>9} {np.mean(plains):>7.2f} {np.mean(carries):>7.2f} {np.mean(naives):>7.2f}"
          f"   (Stage 1 bar 10.54)")
    print(f"\ncarryover vs plain: {np.mean(carries) - np.mean(plains):+.2f} | "
          f"carryover vs naive bar: {np.mean(carries) - np.mean(naives):+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
