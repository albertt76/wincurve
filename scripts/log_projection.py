"""Append the current projection run to the projection history time-series.

Run this on a cadence (roughly monthly in the offseason; a few times in-season, with a run right
after the trade deadline) to build a time series of how the projection for a season moves. The UI
charts the drift per team.

Reads a projection bundle (default the live `data/processed/projections_current.json`) and appends
one compact per-team record per run to `data/projection_history.json`, keyed by (run_date, model):
re-running on the same date and model REPLACES that entry (idempotent), so a mistaken run is
harmless. The history file lives at the repo `data/` root (tracked in git, NOT under the gitignored
`data/processed/`), so the time series persists across checkouts and deploys.

    python scripts/log_projection.py                      # log today's preseason bundle
    python scripts/log_projection.py --model inseason \
        --bundle data/processed/projection_inseason.json  # log an in-season run
    python scripts/log_projection.py --run-date 2026-09-01  # override the run date

What "drift" means depends on the model that produced the bundle, and the UI must label it:
- preseason model: a mid-cycle re-run re-projects from the CURRENT roster using prior-season player
  talent + carryover. So its drift is a ROSTER-COMPOSITION signal (trades, injuries, overrides) --
  NOT how players are actually performing this year (the preseason model has no in-season updating).
- inseason model: folds in results so far, so its drift additionally reflects TEAM FORM.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

PROC = Path("data/processed")
HISTORY = Path("data/projection_history.json")


def _record(bundle: dict, model: str, run_date: str) -> dict:
    """Compact per-team snapshot of a projection run (only what the drift charts need)."""
    teams = [{
        "abbr": t["abbr"],
        "wins": t["wins"],
        "rating": t.get("rating"),
        "off_rating": t.get("off_rating"),
        "def_rating": t.get("def_rating"),
        "turnover": t.get("turnover"),
    } for t in bundle["teams"]]
    teams.sort(key=lambda x: -x["wins"])
    return {
        "run_date": run_date,
        "model": model,
        "season": bundle["meta"]["season"],
        "snapshot_date": bundle["meta"].get("snapshot_date"),
        "teams": teams,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bundle", default=str(PROC / "projections_current.json"),
                    help="projection bundle to log (default the live preseason bundle)")
    ap.add_argument("--model", default="preseason", choices=["preseason", "inseason"],
                    help="which model produced the bundle (labels the drift series)")
    ap.add_argument("--run-date", default=None,
                    help="ISO run date (default the bundle snapshot_date, else today)")
    args = ap.parse_args()

    bundle = json.loads(Path(args.bundle).read_text())
    run_date = args.run_date or bundle["meta"].get("snapshot_date") or date.today().isoformat()
    rec = _record(bundle, args.model, run_date)

    hist = {"runs": []}
    if HISTORY.exists():
        hist = json.loads(HISTORY.read_text())
    # idempotent on (run_date, model, season): drop any existing match, then append.
    hist["runs"] = [r for r in hist["runs"]
                    if not (r["run_date"] == run_date and r["model"] == args.model
                            and r["season"] == rec["season"])]
    hist["runs"].append(rec)
    hist["runs"].sort(key=lambda r: (r["season"], r["run_date"], r["model"]))
    HISTORY.write_text(json.dumps(hist, indent=1))

    n_dates = len({(r["season"], r["run_date"], r["model"]) for r in hist["runs"]})
    print(f"logged {args.model} run for {rec['season']} @ {run_date} "
          f"({len(rec['teams'])} teams) -> {HISTORY}")
    print(f"history now holds {len(hist['runs'])} runs across {n_dates} (season,date,model) points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
