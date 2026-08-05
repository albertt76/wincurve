"""NHL Stage 6: the historical track record -- our model vs the real Vegas market, walk-forward.

Unblocks what DESIGN.md had recorded as blocked: Kalshi's KXNHLWINS has zero settled events (no
history at all), but hockey-reference.com carries hockey-reference.com/leagues/NHL_<year>_
preseason_odds.html -- real preseason POINTS over/under lines (our model's exact target unit) for
every season since 2010-11, which happens to be the exact floor of our own RAPM/shift-chart
backbone (nhl.ingest.FIRST_SHIFT_SEASON). So every season our honest model can ever backtest also
has a real market line to grade against (nhl/odds.py).

This runs the EXACT shipped pipeline (nhl.season: honest roster, sim+carry) on the same walk-forward
folds as the Stage 5 gate, and additionally scores the Vegas line's own MAE on those same folds --
so "do we beat the market" is measured honestly, on identical folds, not cherry-picked.

    python scripts/nhl_market_history_report.py

Legend: MAE in 82-game standings points (lower is better). ours = sim+carry (the shipped model);
vegas = the preseason Vegas points O/U line itself (a naive "trust the market" predictor); naive =
the Stage 1 bar (mean-reverted previous points).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import odds, season, teams  # noqa: E402
from nhl.ingest import season_str  # noqa: E402
from nhl.teams import SHORTENED_SEASONS  # noqa: E402


def naive_bar(P: pd.DataFrame, Y: int, gmean: float) -> float:
    te = season.team_actuals(Y).rename(columns={"pts82": "act"})
    prk = pd.concat([season.team_actuals(y - 1).rename(columns={"pts82": "prev"})[["team", "prev"]]
                     .assign(Y=y) for y in range(int(P["Y"].min()) + 1, Y)])
    trk = P[P["Y"] < Y][["team", "Y", "pts82"]].merge(prk, on=["team", "Y"])
    k = float(np.polyfit(trk["prev"] - gmean, trk["pts82"] - gmean, 1)[0]) if len(trk) > 10 else 0.5
    prev = season.team_actuals(Y - 1).rename(columns={"pts82": "prev"})[["team", "prev"]]
    m = te.merge(prev, on="team")
    return float((gmean + k * (m["prev"] - gmean) - m["act"]).abs().mean())


def main() -> int:
    market = odds.load_market_baseline(teams.target_table())
    vegas_seasons = sorted(market.dropna(subset=["points_ou"])["season_start"].unique())
    print(f"Vegas preseason points O/U available: {season_str(vegas_seasons[0])}.."
          f"{season_str(vegas_seasons[-1])} ({len(vegas_seasons)} seasons)\n")

    years = season.projectable_seasons(last=2025)
    P = season.build_panel(years)
    P = season.walkforward_means(P)
    P = season.add_carry_residual(P)
    gmean = P["pts82"].mean()

    folds = [Y for Y in years if Y >= years[0] + 4 and Y in vegas_seasons]
    print(f"Scoring {len(folds)} folds with BOTH a projectable model and a real Vegas line: "
          f"{', '.join(season_str(f) for f in folds)}\n")

    rows = []
    print(f"{'season':>9} {'ours':>7} {'vegas':>7} {'naive':>7} {'n':>4}")
    for Y in folds:
        te = P[P["Y"] == Y].dropna(subset=["mu"]).copy()
        _, carry = season.carryover(P, Y, "mu")
        te = te.set_index("team")
        pred_ours = te["mu"] + carry

        mkt = market[market["season_start"] == Y].set_index("team")["points_ou"]
        common = te.index.intersection(mkt.dropna().index)
        act = te.loc[common, "pts82"]
        mae_ours = float((pred_ours.loc[common] - act).abs().mean())
        mae_vegas = float((mkt.loc[common] - act).abs().mean())
        mae_naive = naive_bar(P, Y, gmean)

        star = "*" if Y in SHORTENED_SEASONS else " "
        print(f"{season_str(Y) + star:>9} {mae_ours:>7.2f} {mae_vegas:>7.2f} {mae_naive:>7.2f} "
              f"{len(common):>4}")
        rows.append({"Y": Y, "ours": mae_ours, "vegas": mae_vegas, "naive": mae_naive})

    R = pd.DataFrame(rows)
    full = R[~R["Y"].isin(SHORTENED_SEASONS)]
    n_short = len(R) - len(full)
    print()
    print(f"{'mean·all':>9} {R['ours'].mean():>7.2f} {R['vegas'].mean():>7.2f} "
          f"{R['naive'].mean():>7.2f}   (all {len(R)} folds)")
    if n_short:
        print(f"{'mean·full':>9} {full['ours'].mean():>7.2f} {full['vegas'].mean():>7.2f} "
              f"{full['naive'].mean():>7.2f}   (headline; excl {n_short} shortened)")

    beats = (full["ours"] < full["vegas"]).sum()
    print(f"\nheadline over {len(full)} full-season folds:")
    print(f"  our model  = {full['ours'].mean():.2f} points")
    print(f"  Vegas line = {full['vegas'].mean():.2f} points  "
          f"({'we beat' if full['ours'].mean() < full['vegas'].mean() else 'Vegas beats'} "
          f"the market on aggregate, {full['ours'].mean() - full['vegas'].mean():+.2f})")
    print(f"  we were closer to actual than Vegas in {beats}/{len(full)} folds")
    print(f"  naive bar  = {full['naive'].mean():.2f} points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
