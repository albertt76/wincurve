"""NHL Stage 2 heavy pull: per-game shift charts for a season (the xG-RAPM on-ice units).

~1300 games/season, one cached call each, idempotent and resumable. Also caches the
season's MoneyPuck shot file (the xG response). Writes
data/nhl/processed/shifts_<season>.parquet.

    python scripts/nhl_fetch_shifts.py --season 2023
    python scripts/nhl_fetch_shifts.py --season 2023 --season 2024 --season 2022
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nhl import ingest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, action="append", required=True,
                    help="season start year, e.g. 2023 for 2023-24 (repeatable)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    for yr in args.season:
        df = ingest.pull_season_shifts(yr)
        print(f"shifts_{yr}: {len(df):,} shift rows across {df['gameId'].nunique()} games")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
