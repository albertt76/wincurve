"""Canonical generator for the box-informed RAPM parquets the projection blends toward.

For each season with cached play-by-play stints (2013-14 .. present), fit offense/defense RAPM
(`nbaproj.rapm.fit_rapm`, ridge alpha=2000) shrunk toward the **current** box-score impact metric
as its Bayesian prior, and write `data/processed/rapm_<season>_a<alpha>.parquet`. These are read
downstream by `nbaproj.rapm.build_rapm_impact` (the RAPM defensive arm of the turnover blend) and
by `box_vs_rapm_by_player` (the UI's D-arrow flags).

## Why this script exists (2026-08-02)

The RAPM parquets used to be generated as a side effect of `rapm_predict.py` /
`rapm_integration_test.py`, with the box prior AS IT WAS at the time -- i.e. before
position-relative rebounding, rim/hustle tracking, and the BPM position fallback improved the box
defensive metric. So the shipped RAPM arm was anchored to a stale box prior. Re-anchoring on the
CURRENT box defense (this script) cleared the gate: walk-forward 5000-sim win MAE
**7.628 -> 7.591 excl-short, +0.036 +/- 0.017 SE (~2.2 SE), 5/6 folds improved**, coverage held
(`scripts/gate_defense_prior.py`). This is now the one canonical place the RAPM parquets are made,
so a future box-metric change means: regenerate `player_impact.parquet`, then re-run THIS, then
rebuild the bundles -- the prior never goes stale silently again.

The prior is contemporaneous by construction (season s's RAPM uses season s's box impact as its
prior and season s's stints), which is point-in-time-safe: a player's season-s value never uses
anything after season s. `fit_rapm` gives a player with no box estimate that season a zero prior
(plain shrinkage toward league average), so thin-sample players are handled the same as before.

    python scripts/build_rapm.py            # regenerate all seasons (skips existing)
    python scripts/build_rapm.py --refresh  # force-regenerate
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nbaproj.bulk_pbp import segments_for_season  # noqa: E402
from nbaproj.rapm import fit_rapm  # noqa: E402

PROC = Path("data/processed")
FIRST_RAPM_SEASON = 2013
ALPHA = 2000


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="force-regenerate existing parquets")
    ap.add_argument("--last", type=int, default=None, help="last season (default: newest cached)")
    args = ap.parse_args()

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    last = args.last if args.last is not None else int(imp["season_start"].max())

    for s in range(FIRST_RAPM_SEASON, last + 1):
        out = PROC / f"rapm_{s}_a{ALPHA}.parquet"
        if out.exists() and not args.refresh:
            print(f"  {s}: exists, skipping (use --refresh to rebuild)")
            continue
        seg = segments_for_season(s)
        if seg.empty:
            print(f"  {s}: no segments, skipping")
            continue
        prior = imp[imp["season_start"] == s][
            ["player_id", "off_impact", "def_impact"]].rename(
            columns={"off_impact": "off_prior", "def_impact": "def_prior"})
        r = fit_rapm(seg, alpha=float(ALPHA), prior=prior)
        r.to_parquet(out, index=False)
        print(f"  {s}: {len(r)} players (prior on {len(prior)}), "
              f"def_rapm SD {r.def_rapm.std():.2f} -> {out.name}", flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
