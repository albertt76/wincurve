"""Build player_seasons.parquet and player_team_seasons.parquet from game logs.

Every downstream script (stage2_report.py, pull_awards.py, gate scripts) reads these
two tables from data/processed, but nothing writes them there -- run this once after
fetch_all.py to materialize nbaproj.players.build_player_team_seasons() /
build_player_seasons() to disk.

    python scripts/build_player_tables.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbaproj.cache import DATA_DIR  # noqa: E402
from nbaproj.players import build_player_seasons, build_player_team_seasons  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                     datefmt="%H:%M:%S")

out = DATA_DIR / "processed"
out.mkdir(parents=True, exist_ok=True)

pts = build_player_team_seasons()
pts.to_parquet(out / "player_team_seasons.parquet", index=False)
print(f"player_team_seasons: {len(pts)} rows, {pts.season_start.nunique()} seasons")

ps = build_player_seasons()
ps.to_parquet(out / "player_seasons.parquet", index=False)
print(f"player_seasons: {len(ps)} rows, {ps.season_start.nunique()} seasons")
