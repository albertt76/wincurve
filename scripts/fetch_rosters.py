"""Pull historical team rosters (30 calls per season)."""
from __future__ import annotations
import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nbaproj.cache import DATA_DIR
from nbaproj.ingest import pull_rosters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
df = pull_rosters(2016, 2025)
out = DATA_DIR / "processed" / "team_rosters.parquet"
df.to_parquet(out, index=False)
print(f"rosters: {len(df)} rows, {df.SEASON.nunique()} seasons -> {out}")
