"""NHL Stage 3b: the HONEST (non-leaky) team points projection + carryover gate.

Stage 4/5 aggregated projected skaters onto each team's ACTUAL season-Y roster + TOI -- a leaky
upper bound (you cannot know February's roster or realized minutes in October). This re-runs the
exact same pipeline (project(Y-1) -> minute-weighted 5v5 net -> walk-forward net->points
calibration -> +one-year carryover) but weighted by the **point-in-time** roster: each team's
opening-day roster (reconstructed from the first games) at each skater's **prior-season** 5v5 TOI
(nhl.rosters.honest_toi). We report the honest projection beside the leaky bound and the Stage 1
bar so the cost of honesty is explicit.

    python scripts/nhl_stage3b_honest_gate.py                       # widest folds the data supports
    python scripts/nhl_stage3b_honest_gate.py --first 2019 --folds 2023 2024 2025

Legend (all in 82-game-equivalent standings points; lower is better):
  MAE   = mean absolute error (average miss, direction ignored)
  plain = calibrated projection; +carry = plain + rho*(last-season residual); rho fit walk-forward
  naive = the Stage 1 bar = mean-reverted previous points (the gate every stage must beat = 10.54)
  leaky = the same model on the ACTUAL roster+TOI (the Stage 4 upper bound, ~10.10 with carryover)
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import aggregate, projection, rosters  # noqa: E402
from nhl.ingest import PROC, season_id, season_str  # noqa: E402
from nhl.teams import SHORTENED_SEASONS  # noqa: E402  -- lockout/covid seasons, reported separately

REF = pd.read_parquet(PROC / "team_reference.parquet")[["team_id", "tricode"]]


def pts82(Y: int) -> pd.DataFrame:
    """Actual 82-game-equivalent standings points per team for season Y (point% x 164)."""
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)].merge(REF, left_on="teamId", right_on="team_id")
    ts["pts82"] = ts["points"] / (2 * ts["gamesPlayed"]) * 164.0
    return ts[["tricode", "pts82"]].rename(columns={"tricode": "team"})


def impact_seasons() -> set[int]:
    return {int(Path(f).stem.split("_")[1])
            for f in glob.glob(str(PROC / f"impact_*_a{int(projection.rapm.DEFAULT_ALPHA)}.parquet"))}


def build_panel(years: list[int], *, honest: bool) -> pd.DataFrame:
    """Aggregated projected 5v5 net + actual points per team-season over `years`.

    honest=True weights the aggregation by the opening-day roster + prior-season TOI
    (rosters.honest_toi); honest=False is the leaky bound (actual roster + TOI, aggregate's default).
    """
    rows = []
    for Y in years:
        toi = rosters.honest_toi(Y) if honest else None
        g = aggregate.team_ratings(projection.project(Y - 1), Y, toi=toi)[["team", "net"]]
        rows.append(g.merge(pts82(Y), on="team").assign(Y=Y))
    return pd.concat(rows, ignore_index=True)


def score(P: pd.DataFrame, folds: list[int]) -> pd.DataFrame:
    """Walk-forward plain / +carryover / naive-bar MAE per fold (calibration + rho + k all fit on
    strictly-earlier seasons). Returns a per-fold table; the caller prints/aggregates it."""
    gmean = P["pts82"].mean()
    # walk-forward plain calibration net -> pts82, per fold, to define residuals for the carryover
    P = P.copy()
    P["pred"] = np.nan
    for Y in sorted(P["Y"].unique()):
        tr = P[P["Y"] < Y]
        if len(tr) < 10:
            continue
        b1, b0 = np.polyfit(tr["net"], tr["pts82"], 1)
        P.loc[P["Y"] == Y, "pred"] = b0 + b1 * P.loc[P["Y"] == Y, "net"]
    P["resid"] = P["pts82"] - P["pred"]

    out = []
    for Y in folds:
        te = P[P["Y"] == Y].dropna(subset=["pred"]).copy()
        if te.empty:
            continue
        # rho fit on residual pairs strictly before Y (last-season residual -> this-season residual)
        pr = P[P["Y"] < Y].merge(
            P.assign(Y=P["Y"] + 1)[["team", "Y", "resid"]].rename(columns={"resid": "rp"}),
            on=["team", "Y"]).dropna(subset=["resid", "rp"])
        rho = float(np.polyfit(pr["rp"], pr["resid"], 1)[0]) if len(pr) > 10 else 0.0
        prev = P[P["Y"] == Y - 1][["team", "resid"]].rename(columns={"resid": "resid_prev"})
        te = te.merge(prev, on="team", how="left").fillna({"resid_prev": 0.0})
        te["carry"] = te["pred"] + rho * te["resid_prev"]
        # naive bar: mean-reverted previous points, reversion k fit on prior transitions
        prk = pd.concat([pts82(y - 1).rename(columns={"pts82": "prev"}).assign(Y=y)
                         for y in range(min(P["Y"]) + 1, Y)])
        trk = P[P["Y"] < Y].merge(prk, on=["team", "Y"])
        k = float(np.polyfit(trk["prev"] - gmean, trk["pts82"] - gmean, 1)[0]) if len(trk) > 10 else 0.5
        nv = te.merge(pts82(Y - 1).rename(columns={"pts82": "prev"}), on="team")
        out.append({
            "Y": Y, "n": len(te),
            "plain": float((te["pred"] - te["pts82"]).abs().mean()),
            "carry": float((te["carry"] - te["pts82"]).abs().mean()),
            "naive": float((gmean + k * (nv["prev"] - gmean) - nv["pts82"]).abs().mean()),
            "rho": rho,
        })
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", type=int, default=None,
                    help="first season Y to include in the panel (default: widest the data supports)")
    ap.add_argument("--last", type=int, default=2025)
    ap.add_argument("--folds", type=int, nargs="+", default=None,
                    help="seasons to score (default: all but the earliest ~4 training years)")
    args = ap.parse_args()

    # A season Y is projectable if project(Y-1) has pooled RAPM (>=1 impact season in [Y-3..Y-1])
    # AND we have shifts_Y (opening roster) AND actual points for Y.
    imp = impact_seasons()
    have_shifts = {int(Path(f).stem.split("_")[1]) for f in glob.glob(str(PROC / "shifts_*.parquet"))}
    projectable = [Y for Y in range((args.first or 2011), args.last + 1)
                   if any(s in imp for s in range(Y - 3, Y)) and Y in have_shifts]
    if not projectable:
        print("no projectable seasons yet -- need impact_<yr> caches and shifts_<Y>. "
              f"have impacts {sorted(imp)}, shifts {sorted(have_shifts)}")
        return 1
    years = projectable
    folds = args.folds or [Y for Y in years if Y >= years[0] + 4]  # leave early seasons to train
    if not folds:
        folds = years[-min(3, len(years)):]

    print(f"Panel seasons: {season_str(years[0])}..{season_str(years[-1])} ({len(years)}); "
          f"scoring folds {', '.join(season_str(f) for f in folds)}\n")

    P_honest = build_panel(years, honest=True)
    P_leaky = build_panel(years, honest=False)
    H = score(P_honest, folds)
    L = score(P_leaky, folds)

    print("HONEST roster (opening-day + prior-season TOI) vs the LEAKY bound and the Stage 1 bar.")
    print("(* = lockout/covid-shortened season -- pt%-normalized but noisier, excluded from the headline)\n")
    print(f"{'season':>9} {'H-plain':>8} {'H-carry':>8} {'L-carry':>8} {'naive':>7} {'rho':>5} {'n':>4}")
    for _, r in H.iterrows():
        lc = L.loc[L["Y"] == r["Y"], "carry"]
        lc = float(lc.iloc[0]) if len(lc) else float("nan")
        star = "*" if int(r["Y"]) in SHORTENED_SEASONS else " "
        print(f"{season_str(int(r['Y']))+star:>9} {r['plain']:>8.2f} {r['carry']:>8.2f} "
              f"{lc:>8.2f} {r['naive']:>7.2f} {r['rho']:>5.2f} {int(r['n']):>4}")

    full = ~H["Y"].isin(SHORTENED_SEASONS)          # full-season folds only -> the headline
    Lf = L[~L["Y"].isin(SHORTENED_SEASONS)]
    n_short = int((~full).sum())

    def line(tag, mask_H, mask_L):
        return (f"{tag:>9} {H.loc[mask_H,'plain'].mean():>8.2f} {H.loc[mask_H,'carry'].mean():>8.2f} "
                f"{L.loc[mask_L,'carry'].mean():>8.2f} {H.loc[mask_H,'naive'].mean():>7.2f}")

    print(line("mean·all", H["Y"].notna(), L["Y"].notna()) + f"   (all {len(H)} folds)")
    if n_short:
        print(line("mean·full", full, ~L["Y"].isin(SHORTENED_SEASONS))
              + f"   (Stage 1 bar 10.54; excl {n_short} shortened)")

    Hf = H[full]
    dh = Hf["carry"].mean() - Hf["naive"].mean()
    db = Hf["carry"].mean() - 10.54
    dl = Hf["carry"].mean() - Lf["carry"].mean()
    print(f"\nheadline = honest+carry over {len(Hf)} full-season folds = {Hf['carry'].mean():.2f} points")
    print(f"  vs Stage 1 bar (10.54):    {db:+.2f}  ({'beats' if db < 0 else 'trails'} the fixed bar)")
    print(f"  vs walk-forward naive:     {dh:+.2f}  ({'beats' if dh < 0 else 'trails'} the per-fold bar)")
    print(f"  vs leaky+carry bound:      {dl:+.2f}  (cost of removing the roster/TOI leak)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
