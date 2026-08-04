"""NHL Stage 0: full historical data pull (idempotent, resumable).

Pulls the foundational datasets -- franchise reference, per-season rule index,
team-season records, and MoneyPuck expected-goals (xG) at team/skater/goalie level --
into data/nhl/processed. Every call is disk-cached, so re-runs are free and a failed
run resumes where it stopped. Per-game play-by-play + shift charts (the RAPM inputs)
are a separate, heavier pull that belongs to the impact stage.

    python scripts/nhl_fetch_all.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nhl import ingest  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    written = ingest.pull_all()
    print("\n=== NHL Stage 0 pull complete (rows written to data/nhl/processed) ===")
    for name, rows in written.items():
        print(f"  {name:24s} {rows:>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
