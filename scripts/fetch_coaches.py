"""Pull head-coach assignments per team-season."""
from __future__ import annotations
import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nbaproj.cache import DATA_DIR
from nbaproj.ingest import pull_coaches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
df = pull_coaches(2013, 2025)
out = DATA_DIR / "processed" / "team_coaches.parquet"
df.to_parquet(out, index=False)
head = df[df.COACH_TYPE == "Head Coach"]
print(f"coaches: {len(df)} rows ({len(head)} head-coach rows), "
      f"{df.SEASON_START.nunique()} seasons -> {out}")
