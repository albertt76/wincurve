"""Compute + cache single-season xG-RAPM impacts (impact_<yr>_a<alpha>.parquet).

The single-season RAPM caches are the input to the aging curve (nhl.aging.rapm_panel) and, via
the birthdate pull, to the forward projection. nhl_build_impact_ui.py computes them as a side
effect of building the UI, but that needs every season present; this extracts just the compute so
it can run incrementally as the shift pull lands seasons. Idempotent and resumable -- a season whose
cache exists is skipped unless --refresh.

    python scripts/nhl_build_impacts.py --season 2023 --season 2024   # specific seasons
    python scripts/nhl_build_impacts.py                               # every pulled shift season
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nhl import rapm  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402


def pulled_seasons() -> list[int]:
    """Seasons with a shift-chart parquet on disk (i.e. RAPM-computable)."""
    return sorted(int(Path(f).stem.split("_")[1])
                  for f in glob.glob(str(PROC / "shifts_*.parquet")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, action="append", help="season start year (repeatable)")
    ap.add_argument("--alpha", type=float, default=rapm.DEFAULT_ALPHA)
    ap.add_argument("--refresh", action="store_true", help="recompute even if the cache exists")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    seasons = args.season or pulled_seasons()
    for yr in seasons:
        out = PROC / f"impact_{yr}_a{int(args.alpha)}.parquet"
        if out.exists() and not args.refresh:
            logging.info("impact_%d_a%d exists -- skip", yr, int(args.alpha))
            continue
        sk = rapm.season_rapm(yr, alpha=args.alpha)
        sk.to_parquet(out, index=False)
        print(f"impact_{season_str(yr)}: {len(sk)} skaters over the {int(sk['toi_min'].sum()/60):,}h "
              f"5v5 TOI floor -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
