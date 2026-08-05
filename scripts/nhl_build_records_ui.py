"""Build the NHL "Records" page: the upcoming-season projected standings, self-contained.

Reads the live projection bundle (`data/nhl/processed/projection_current.json`, written by
`scripts/nhl_project_current.py`) -- each team's projected standings points as a mean + calibrated
80% interval, with the offense / defense / carryover drivers -- and inlines it into
`ui/nhl_records/template.html` (a `__DATA__` placeholder) to produce a single dependency-free
`ui/nhl_records/records.html`, mirroring the NBA `ui/build.py` and the NHL impact-viewer build.

Market lines (Kalshi `KXNHLWINS`) are attached when posted, strictly for display beside the
projection -- never an input. As of the 2026-27 pre-season the market has no open events, so the
page shows no ring and says so; re-running once it opens attaches it automatically.

    python scripts/nhl_project_current.py       # (re)build the projection bundle first
    python scripts/nhl_build_records_ui.py       # inline it into ui/nhl_records/records.html
    python scripts/nhl_build_records_ui.py --market   # also try to attach the live market wins
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nhl.ingest import PROC  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "ui" / "nhl_records" / "template.html"
OUTPUT = ROOT / "ui" / "nhl_records" / "records.html"
BUNDLE = PROC / "projection_current.json"


def attach_market(bundle: dict) -> int:
    """Best-effort: attach the live Kalshi implied WIN table to each team (downstream display only).
    Returns the number of teams matched; 0 (and unchanged) when nothing is posted.

    Kalshi settles on WINS, but the Records page's shared axis is standings POINTS -- plotting a win
    total straight onto a points axis would silently mix units. Standings points = 2*wins + OT-loss
    games, and we don't have the market's own implied OT-loss split, so we reuse OUR model's implied
    OT-loss count for that team (``proj_points - 2*wins``, both already in the bundle) as the
    conversion factor: ``mkt`` (points-equivalent, for the axis) = ``2*mkt_wins + our_otl``. This is a
    documented approximation -- it assumes the market agrees with us on how a team's decided-in-OT
    share splits, which is the only information we have. ``mkt_wins`` (the raw market number) is kept
    alongside it for an honest readout.
    """
    try:
        from nhl import market_live
        yy = (bundle["meta"]["target_start"] + 1) % 100
        table = market_live.market_win_table(yy)
    except Exception as err:  # noqa: BLE001 -- market is optional; never block the build
        print(f"market: skipped ({type(err).__name__}: {err})")
        return 0
    n = 0
    for t in bundle["teams"]:
        m = table.get(t["team"])
        if m and m.get("mean") is not None:
            mkt_wins = float(m["mean"])
            our_otl = t["proj"] - 2 * t["wins"]         # our model's implied OT-loss games this team
            t["mkt_wins"] = round(mkt_wins, 1)
            t["mkt"] = round(2 * mkt_wins + our_otl, 1)  # points-equivalent, for the shared axis
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", action="store_true", help="also try to attach live Kalshi win lines")
    args = ap.parse_args()

    if not BUNDLE.exists():
        print(f"missing {BUNDLE.relative_to(ROOT)} -- run scripts/nhl_project_current.py first")
        return 1
    bundle = json.loads(BUNDLE.read_text())

    if args.market:
        n = attach_market(bundle)
        print(f"market: attached {n} team win lines" if n else "market: not posted yet (no ring)")

    tpl = TEMPLATE.read_text()
    html = tpl.replace("__DATA__", json.dumps(bundle, separators=(",", ":")))
    OUTPUT.write_text(html)
    kb = len(html.encode()) / 1024
    print(f"wrote {OUTPUT.relative_to(ROOT)}  ({kb:.0f} KB, {len(bundle['teams'])} teams, "
          f"{bundle['meta']['target_season']}, snapshot {bundle['meta']['snapshot_date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
