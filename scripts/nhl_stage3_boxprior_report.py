"""NHL Stage 3, step 2: box-informed OFFENSIVE prior on top of pooled xG-RAPM.

Pooled RAPM (step 1) already beats single-season predictively. This tests whether blending a
box-score OFFENSIVE prior -- each skater's own individual expected goals per 60 (MoneyPuck
`I_F_xGoals`, 5v5), which is largely linemate-independent -- improves next-season prediction
further. Defense has no comparable individual box stat in hockey, so the prior is offense-only.

Method (leakage-free, same as step 1): predictor measured through season Y-1, target = the player's
actual single-season `net` in Y. We sweep the blend weight on the box offense and report the
correlation vs pure pooled RAPM (w=0), over every target with a pooled cache available.

    python scripts/nhl_stage3_boxprior_report.py --targets 2019 2020 2021 2022 2023 2024

Legend: RAPM = regularized adjusted plus-minus; I_F_xGoals = a skater's individual expected goals
(own shots); off/def/net = xG per 60 (5v5); corr = Pearson correlation; TOI = time on ice.
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


def single_net(year: int, alpha: float) -> pd.DataFrame:
    return pd.read_parquet(PROC / f"impact_{year}_a{int(alpha)}.parquet")


def pooled(end_year: int, window: int, decay: float, alpha: float) -> pd.DataFrame:
    cache = PROC / f"pool_{end_year}_w{window}_d{decay}_a{int(alpha)}.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    df = rapm.pool_rapm(end_year, window=window, decay=decay, alpha=alpha)
    df.to_parquet(cache, index=False)
    return df


box_offense = rapm.box_offense_prior  # canonical impl lives in nhl.rapm (used by the talent pipeline)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, nargs="+", default=[2019, 2020, 2021, 2022, 2023, 2024])
    ap.add_argument("--window", type=int, default=rapm.DEFAULT_WINDOW)
    ap.add_argument("--decay", type=float, default=rapm.DEFAULT_DECAY)
    ap.add_argument("--alpha", type=float, default=rapm.DEFAULT_ALPHA)
    ap.add_argument("--weights", type=float, nargs="+", default=[0.0, 0.3, 0.4, 0.5, 0.6])
    args = ap.parse_args()

    print("Box-informed OFFENSIVE prior (individual xG) blended onto pooled xG-RAPM offense.")
    print(f"window={args.window}, decay={args.decay}, alpha={args.alpha:g}; "
          f"predictor through Y-1 -> target = single-season net in Y.\n")

    per_fold = {w: {} for w in args.weights}
    for Y in args.targets:
        try:
            pool = pooled(Y - 1, args.window, args.decay, args.alpha)[["player_id", "off", "def"]]
        except Exception as err:  # noqa: BLE001
            print(f"  (skip {season_str(Y)}: {err})"); continue
        tgt = single_net(Y, args.alpha)[["player_id", "net"]].rename(columns={"net": "tnet"})
        df = pool.merge(box_offense(Y - 1, args.window), on="player_id").merge(tgt, on="player_id")
        zb = (df["box_ioff"] - df["box_ioff"].mean()) / df["box_ioff"].std()
        df["bscaled"] = zb * df["off"].std() + df["off"].mean()   # box offense on the RAPM-off scale
        for w in args.weights:
            pred = (1 - w) * df["off"] + w * df["bscaled"] + df["def"]
            per_fold[w][Y] = float(np.corrcoef(pred, df["tnet"])[0, 1])

    folds = sorted(set().union(*[set(per_fold[w]) for w in args.weights]))
    hdr = "".join(f"{season_str(Y):>9}" for Y in folds)
    print(f"{'w_box':>6}{hdr}{'mean':>8}{'vs w=0':>8}{'folds+':>8}")
    base = {Y: per_fold[0.0][Y] for Y in folds}
    for w in args.weights:
        vals = [per_fold[w][Y] for Y in folds]
        m = float(np.mean(vals))
        nup = sum(per_fold[w][Y] > base[Y] for Y in folds)
        cells = "".join(f"{per_fold[w][Y]:>9.3f}" for Y in folds)
        print(f"{w:>6.2f}{cells}{m:>8.3f}{m - np.mean(list(base.values())):>+8.3f}{nup:>6}/{len(folds)}")

    best = max(args.weights[1:], key=lambda w: np.mean([per_fold[w][Y] for Y in folds]))
    dm = np.mean([per_fold[best][Y] for Y in folds]) - np.mean(list(base.values()))
    print(f"\n  best blend w={best}: mean +{dm:.3f} vs pure pooled RAPM over {len(folds)} folds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
