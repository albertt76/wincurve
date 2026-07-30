"""Pull rosters + coaches for the upcoming season (60 calls)."""
from __future__ import annotations
import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nbaproj.cache import DATA_DIR
from nbaproj.ingest import pull_rosters, pull_coaches

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
TARGET = 2026
r = pull_rosters(TARGET, TARGET)
c = pull_coaches(TARGET, TARGET)
r.to_parquet(DATA_DIR / "processed" / "rosters_current.parquet", index=False)
c.to_parquet(DATA_DIR / "processed" / "coaches_current.parquet", index=False)
print(f"rosters {len(r)} rows / {r.TeamID.nunique()} teams; "
      f"coaches {len(c)} rows, {len(c[c.COACH_TYPE=='Head Coach'])} head coaches")
