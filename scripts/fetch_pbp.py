"""Pull stint segments for RAPM, one season at a time. Resumable and fault-tolerant.

Each game's GameRotation and play-by-play are cached individually, so an interrupted or
rate-limited run resumes for free: re-running only fetches what is missing. Games that fail
after retries are logged and skipped rather than killing the run; a second pass picks them
up once the cache is warm.

stats.nba.com rate-limits GameRotation hard, so this is a slow background job by design --
the project's design doc always flagged the RAPM pull as an overnight effort.

    python scripts/fetch_pbp.py --season 2024        # 2024-25
    python scripts/fetch_pbp.py --season 2024 --limit 100   # first 100 games (a probe)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nbaproj.cache import DATA_DIR, season_str  # noqa: E402
from nbaproj.ingest import game_log  # noqa: E402
from nbaproj.pbp import build_segments  # noqa: E402

log = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True, help="season start year")
    ap.add_argument("--limit", type=int, default=None, help="cap games (for a probe)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    season = season_str(args.season)
    gl = game_log(season)
    game_ids = sorted(gl["GAME_ID"].astype(str).unique())
    if args.limit:
        game_ids = game_ids[:args.limit]
    log.info("%s: %d games to process", season, len(game_ids))

    out_dir = DATA_DIR / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_path = out_dir / f"segments_{args.season}.parquet"
    done_path = out_dir / f"segments_{args.season}_done.txt"

    done: set[str] = set()
    if done_path.exists():
        done = set(done_path.read_text().split())
    frames: list[pd.DataFrame] = []
    if seg_path.exists():
        frames.append(pd.read_parquet(seg_path))

    failures, processed = [], 0
    for i, gid in enumerate(game_ids, 1):
        if gid in done:
            continue
        try:
            seg = build_segments(gid)
            if not seg.empty:
                frames.append(seg)
            done.add(gid)
            processed += 1
        except Exception as err:  # noqa: BLE001 - one bad game must not kill the run
            failures.append(gid)
            log.warning("game %s failed: %s", gid, type(err).__name__)

        # Checkpoint every 25 games so progress survives an interruption.
        if processed and processed % 25 == 0:
            if frames:
                pd.concat(frames, ignore_index=True).to_parquet(seg_path, index=False)
            done_path.write_text(" ".join(sorted(done)))
            log.info("checkpoint: %d/%d done, %d failed, %d segments",
                     len(done), len(game_ids), len(failures),
                     sum(len(f) for f in frames))
        time.sleep(0.3)  # small extra spacing on top of the cache throttle

    if frames:
        allseg = pd.concat(frames, ignore_index=True)
        allseg.to_parquet(seg_path, index=False)
    else:
        allseg = pd.DataFrame()
    done_path.write_text(" ".join(sorted(done)))

    print(f"\n{season}: {len(done)}/{len(game_ids)} games, "
          f"{len(failures)} failed, {len(allseg)} segments -> {seg_path}")
    if failures:
        print(f"  failed games (re-run to retry): {failures[:10]}"
              f"{' ...' if len(failures) > 10 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
