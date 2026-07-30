"""Fetch the full historical dataset into data/raw and data/processed.

Idempotent and resumable: everything goes through the disk cache, so re-running
after an interruption only fetches what's missing.

    python scripts/fetch_all.py            # 2005-06 .. 2024-25
    python scripts/fetch_all.py --last 2024
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbaproj.cache import DATA_DIR  # noqa: E402
from nbaproj.ingest import LAST_COMPLETE_SEASON, pull_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=LAST_COMPLETE_SEASON,
                    help="last season start year (inclusive)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir = DATA_DIR / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)

    datasets = pull_all(last_season=args.last)

    print(f"\n{'dataset':<18} {'rows':>8} {'seasons':>8}  span")
    print("-" * 52)
    for name, df in datasets.items():
        if df.empty:
            print(f"{name:<18} {'EMPTY':>8}")
            continue
        df.to_parquet(out_dir / f"{name}.parquet", index=False)
        span = f"{df['SEASON'].min()} .. {df['SEASON'].max()}"
        print(f"{name:<18} {len(df):>8,} {df['SEASON'].nunique():>8}  {span}")

    print(f"\nwrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
