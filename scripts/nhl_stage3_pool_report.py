"""NHL Stage 3, step 1: multi-season pooled xG-RAPM vs single-season.

The known single-season limitation (DESIGN.md): xG-RAPM over-credits a player for strong
linemates over one season. The fix is to pool a trailing window of seasons with recency decay,
so a player who really drives play -- across changing linemates -- separates from one season's
context. This report validates that fix two ways, PREDICTIVELY (not on an internal diagnostic,
per the project's rule):

  1. Next-season predictiveness (the real test): does a metric measured through season Y predict
     each player's ACTUAL season Y+1 on-ice results better? We use the player's Y+1 single-season
     `net` as the (noisy but unbiased) target and compare correlations -- both predictors face the
     same target, so the higher correlation captures more persistent skill and less noise.
  2. corr(net, TOI): a good talent metric ranks players who play more (stars) higher; single-season
     RAPM's depth-player spikes weaken this. (Diagnostic only, reported for color.)

Pooled fits are cached to data/nhl/processed/pool_<end>_w<window>_d<decay>_a<alpha>.parquet.

    python scripts/nhl_stage3_pool_report.py                    # default window=3, decay=0.75
    python scripts/nhl_stage3_pool_report.py --targets 2022 2023 2024 --window 3 --decay 0.75

Legend: RAPM = regularized adjusted plus-minus (ridge that credits on-ice impact controlling for
teammates+opponents); net = off+def xG per 60 (5v5); corr = Pearson correlation; TOI = time on ice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import rapm  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402


def single_rapm(year: int, alpha: float) -> pd.DataFrame:
    """Cached single-season RAPM (the viewer's impact_<yr>_a<alpha>.parquet)."""
    cache = PROC / f"impact_{year}_a{int(alpha)}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    df = rapm.season_rapm(year, alpha=alpha)
    df.to_parquet(cache, index=False)
    return df


def pooled_rapm(end_year: int, window: int, decay: float, alpha: float) -> pd.DataFrame:
    """Cached multi-season pooled RAPM ending at end_year."""
    cache = PROC / f"pool_{end_year}_w{window}_d{decay}_a{int(alpha)}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    df = rapm.pool_rapm(end_year, window=window, decay=decay, alpha=alpha)
    df.to_parquet(cache, index=False)
    return df


def _corr(pred: pd.DataFrame, target: pd.DataFrame) -> tuple[float, int]:
    """corr(pred.net, target.net) over players in both, with the overlap count."""
    m = pred[["player_id", "net"]].merge(
        target[["player_id", "net"]], on="player_id", suffixes=("_p", "_t"))
    if len(m) < 10:
        return float("nan"), len(m)
    return float(np.corrcoef(m["net_p"], m["net_t"])[0, 1]), len(m)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, nargs="+", default=[2022, 2023, 2024],
                    help="season Y whose result we predict from metrics measured through Y-1")
    ap.add_argument("--window", type=int, default=rapm.DEFAULT_WINDOW)
    ap.add_argument("--decay", type=float, default=rapm.DEFAULT_DECAY)
    ap.add_argument("--alpha", type=float, default=rapm.DEFAULT_ALPHA)
    args = ap.parse_args()

    print(__doc__.split("Legend:")[0].strip().split("\n\n")[0])
    print(f"\nwindow={args.window} seasons, recency decay={args.decay}, ridge alpha={args.alpha:g}\n")

    print("== next-season predictiveness: corr(predictor through Y-1, actual single-season net in Y) ==")
    print(f"{'target Y':>10} {'single(Y-1)':>12} {'pooled(Y-1)':>12} {'Δ pooled-single':>16} {'players':>8}")
    rows = []
    for Y in args.targets:
        tgt = single_rapm(Y, args.alpha)
        s1 = single_rapm(Y - 1, args.alpha)
        p1 = pooled_rapm(Y - 1, args.window, args.decay, args.alpha)
        # Score BOTH predictors on the identical common player set (single ⊂ pooled otherwise),
        # so the reported gap is a fair, same-sample comparison.
        common = set(s1["player_id"]) & set(p1["player_id"]) & set(tgt["player_id"])
        cs, _ = _corr(s1[s1["player_id"].isin(common)], tgt)
        cp, npl = _corr(p1[p1["player_id"].isin(common)], tgt)
        rows.append((cs, cp))
        print(f"{season_str(Y):>10} {cs:>12.3f} {cp:>12.3f} {cp - cs:>+16.3f} {npl:>8}")
    cs_m = np.nanmean([r[0] for r in rows]); cp_m = np.nanmean([r[1] for r in rows])
    print(f"{'mean':>10} {cs_m:>12.3f} {cp_m:>12.3f} {cp_m - cs_m:>+16.3f}")
    verdict = "pooling PREDICTS BETTER" if cp_m > cs_m else "pooling does NOT beat single-season"
    print(f"\n  -> {verdict} (mean Δ = {cp_m - cs_m:+.3f} correlation)")

    print("\n== corr(net, TOI) diagnostic (higher = stars rank higher, fewer depth-player spikes) ==")
    print(f"{'end year':>10} {'single':>10} {'pooled':>10}")
    for Y in args.targets:
        rs = np.corrcoef((s := single_rapm(Y, args.alpha))["net"], s["toi_min"])[0, 1]
        rp = np.corrcoef((p := pooled_rapm(Y, args.window, args.decay, args.alpha))["net"], p["toi_min"])[0, 1]
        print(f"{season_str(Y):>10} {rs:>10.3f} {rp:>10.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
