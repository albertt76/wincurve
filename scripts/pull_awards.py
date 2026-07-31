"""Pull All-NBA / All-Defensive selections (PlayerAwards) for the honoree-candidate pool.

Honorees are always significant-minute players, so the candidate set -- the union of the
top-75 by minutes each season plus anyone over 1500 minutes -- captures them while keeping the
number of (cached, throttled) API calls modest. One call per player returns their full award
history; we keep only the All-NBA and All-Defensive rows. Writes data/processed/player_awards.parquet.

    python scripts/pull_awards.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nbaproj.ingest import player_awards  # noqa: E402

PROC = Path("data/processed")
FIRST = 2012  # need prior-year honors back far enough for the earliest correction fold


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ps = pd.read_parquet(PROC / "player_seasons.parquet")
    ps = ps[ps["season_start"] >= FIRST]
    cand = set()
    for _, sub in ps.groupby("season_start"):
        cand |= set(sub.nlargest(75, "minutes")["player_id"])
    cand |= set(ps[ps["minutes"] >= 1500]["player_id"])
    cand = sorted(int(p) for p in cand if pd.notna(p))
    print(f"pulling awards for {len(cand)} candidate players (top-75/season + >=1500 min)")

    frames, miss = [], 0
    for i, pid in enumerate(cand, 1):
        try:
            df = player_awards(pid)
        except Exception as e:  # noqa: BLE001
            miss += 1
            logging.warning("awards %s failed: %s", pid, e)
            continue
        if df is not None and len(df):
            frames.append(df)
        if i % 50 == 0:
            print(f"  {i}/{len(cand)} pulled")

    allaw = pd.concat(frames, ignore_index=True)
    honors = allaw[allaw["DESCRIPTION"].isin(["All-NBA", "All-Defensive Team"])].copy()
    honors["season_start"] = honors["SEASON"].str[:4].astype(int)
    honors["team_number"] = pd.to_numeric(honors["ALL_NBA_TEAM_NUMBER"], errors="coerce")
    keep = honors[["PERSON_ID", "DESCRIPTION", "team_number", "SEASON", "season_start"]].rename(
        columns={"PERSON_ID": "player_id"}).drop_duplicates()
    out = PROC / "player_awards.parquet"
    keep.to_parquet(out, index=False)
    ad = keep[keep["DESCRIPTION"] == "All-Defensive Team"]
    an = keep[keep["DESCRIPTION"] == "All-NBA"]
    print(f"\nWROTE {out}  ({miss} players errored)")
    print(f"  All-Defensive rows: {len(ad)}  seasons {ad.season_start.min()}-{ad.season_start.max()}")
    print(f"  All-NBA rows:       {len(an)}  seasons {an.season_start.min()}-{an.season_start.max()}")
    print(f"  per-season All-Def counts (should be ~10): "
          f"{ad[ad.season_start>=2015].groupby('season_start').size().to_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
