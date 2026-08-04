"""Pull player birthdates (for Stage 3 aging curves) -> data/nhl/processed/player_birthdates.parquet.

Age is the prerequisite for aging curves and MoneyPuck carries no birthdate, so we pull each
player's NHL 'landing' bio record (one cached call each) for everyone who appears in the
single-season xG-RAPM caches (impact_<yr>_a<alpha>.parquet). Idempotent and resumable: every call
is disk-cached, and players already in the parquet are skipped unless --refresh.

    python scripts/nhl_fetch_birthdates.py
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nhl import ingest  # noqa: E402
from nhl.ingest import PROC  # noqa: E402

log = logging.getLogger("birthdates")
OUT = PROC / "player_birthdates.parquet"


def impact_player_ids() -> set[int]:
    ids: set[int] = set()
    for f in glob.glob(str(PROC / "impact_*_a*.parquet")):
        ids |= set(pd.read_parquet(f, columns=["player_id"])["player_id"].astype(int))
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-pull all birthdates")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    want = impact_player_ids()
    have: dict[int, str] = {}
    if OUT.exists() and not args.refresh:
        prev = pd.read_parquet(OUT)
        have = dict(zip(prev["player_id"].astype(int), prev["birthDate"]))
    todo = sorted(want - set(have))
    log.info("birthdates: %d players, %d already cached, pulling %d", len(want), len(have), len(todo))

    rows = [{"player_id": pid, "birthDate": bd} for pid, bd in have.items()]
    for i, pid in enumerate(todo):
        try:
            bd = ingest.player_landing(pid).get("birthDate")
        except Exception as err:  # noqa: BLE001 -- a missing/renamed player id shouldn't abort
            log.warning("  landing %d failed: %s", pid, err)
            bd = None
        rows.append({"player_id": pid, "birthDate": bd})
        if (i + 1) % 200 == 0:
            log.info("  %d/%d", i + 1, len(todo))

    df = pd.DataFrame(rows).drop_duplicates("player_id")
    df.to_parquet(OUT, index=False)
    n_ok = int(df["birthDate"].notna().sum())
    log.info("wrote %s: %d players, %d with birthDate", OUT.name, len(df), n_ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
