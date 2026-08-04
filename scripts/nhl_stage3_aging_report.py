"""NHL Stage 3, step 3: aging curves for xG-RAPM impact, per skill.

Prints, for offense / defense / net, the TOI-weighted average year-over-year change by age (the
delta curve) and its integral (the level curve, ref age 27 = 0), plus the implied peak age. See
nhl/aging.py for the delta-method details and the survivorship caveat.

    python scripts/nhl_stage3_aging_report.py

Requires data/nhl/processed/player_birthdates.parquet (scripts/nhl_fetch_birthdates.py) and the
single-season RAPM caches impact_<yr>_a3000.parquet. Legend: off/def/net = xG per 60 (5v5); delta =
avg change to the next season; level = cumulative delta (relative talent vs age 27); n = player-pairs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nhl import aging  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=3000.0)
    args = ap.parse_args()

    print("NHL xG-RAPM aging curves (delta method, TOI-weighted, single-season impact).")
    print("Smoothed: a degree-2 polynomial fit to the noisy per-age deltas, then integrated.")
    print("sdelta = smoothed avg change to next season; level = talent vs age 27 (higher=better).")
    print("Survivorship caveat: decliners drop out, so the old-age fall-off is UNDERSTATED.\n")

    for metric in ("off", "def", "net"):
        raw = aging.curve(metric, alpha=args.alpha)
        tbl, peak = aging.smooth_curve(metric, alpha=args.alpha, degree=2)
        shape = "declines from youngest age" if peak <= 20 else f"peak age {peak}"
        print(f"== {metric.upper()} ({shape}) ==")
        print(f"{'age':>4} {'n':>6} {'raw_d':>8} {'sdelta':>8} {'level':>8}")
        rawd = dict(zip(raw["age"], raw["delta"]))
        rawn = dict(zip(raw["age"], raw["n_pairs"]))
        for _, r in tbl.iterrows():
            a = int(r["age"]); mark = "  <- peak" if a == peak else ""
            rd = f"{rawd[a]:>+8.3f}" if a in rawd else f"{'-':>8}"
            n = f"{int(rawn[a]):>6}" if a in rawn else f"{'-':>6}"
            print(f"{a:>4} {n} {rd} {r['sdelta']:>+8.3f} {r['slevel']:>+8.3f}{mark}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
