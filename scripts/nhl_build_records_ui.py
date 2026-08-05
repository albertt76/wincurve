"""Build the NHL "Records" page: the upcoming-season projected standings, self-contained.

Reads the live projection bundle (`data/nhl/processed/projection_current.json`, written by
`scripts/nhl_project_current.py`) -- each team's projected standings points as a mean + calibrated
80% interval, with the offense / defense / carryover drivers -- and inlines it into
`ui/nhl_records/template.html` (a `__DATA__` placeholder) to produce a single dependency-free
`ui/nhl_records/records.html`, mirroring the NBA `ui/build.py` and the NHL impact-viewer build.

Market lines are attached when available, strictly for display beside the projection -- never an
input. Two sources, tried in order (source-aware, like the NBA project's `marketLabel`):

1. **Vegas points** (`nhl.market_vegas`) -- a real sportsbook's season POINTS total, our model's
   exact target unit, so no conversion is needed. Preferred when present. As of 2026-08 this is
   BetOnline via a hand-curated gambling911.com recap (see `market_vegas.LIVE_SOURCES` -- it must
   be re-found and added each season, there is no stable per-season URL the way hockey-reference's
   historical archive has).
2. **Kalshi implied WINS** (`nhl.market_live`, `KXNHLWINS`) -- used only for a team Vegas didn't
   cover. Kalshi settles on wins, not points, so it is converted to a points-equivalent via that
   team's own implied OT-loss share (documented in `attach_kalshi`). As of the 2026-27 pre-season
   the series has zero open events, so this path is currently a no-op (kept for when it posts).

    python scripts/nhl_project_current.py       # (re)build the projection bundle first
    python scripts/nhl_build_records_ui.py       # inline it into ui/nhl_records/records.html
    python scripts/nhl_build_records_ui.py --market   # also try to attach live market lines
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


def attach_vegas(bundle: dict) -> int:
    """Best-effort: attach a live Vegas POINTS total per team. Returns teams matched."""
    try:
        from nhl import market_vegas
        table = market_vegas.live_points_table(bundle["meta"]["target_start"])
    except Exception as err:  # noqa: BLE001 -- market is optional; never block the build
        print(f"vegas market: skipped ({type(err).__name__}: {err})")
        return 0
    n = 0
    for t in bundle["teams"]:
        m = table.get(t["team"])
        if m:
            t["mkt"] = round(float(m["points_ou"]), 1)   # already points -- plots directly
            t["mkt_source"] = "vegas"
            t["mkt_book"] = m["source"]
            n += 1
    return n


def attach_kalshi(bundle: dict) -> int:
    """Best-effort: attach the live Kalshi implied WIN table for teams `attach_vegas` didn't cover.
    Returns teams newly matched.

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
        print(f"kalshi market: skipped ({type(err).__name__}: {err})")
        return 0
    n = 0
    for t in bundle["teams"]:
        if t.get("mkt") is not None:
            continue  # Vegas already covered this team
        m = table.get(t["team"])
        if m and m.get("mean") is not None:
            mkt_wins = float(m["mean"])
            our_otl = t["proj"] - 2 * t["wins"]         # our model's implied OT-loss games this team
            t["mkt_wins"] = round(mkt_wins, 1)
            t["mkt"] = round(2 * mkt_wins + our_otl, 1)  # points-equivalent, for the shared axis
            t["mkt_source"] = "kalshi"
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", action="store_true", help="also try to attach live market lines")
    args = ap.parse_args()

    if not BUNDLE.exists():
        print(f"missing {BUNDLE.relative_to(ROOT)} -- run scripts/nhl_project_current.py first")
        return 1
    bundle = json.loads(BUNDLE.read_text())

    if args.market:
        nv = attach_vegas(bundle)
        nk = attach_kalshi(bundle)
        print(f"market: {nv} teams from Vegas, {nk} from Kalshi" if nv or nk
              else "market: no source posted yet (no ring)")

    tpl = TEMPLATE.read_text()
    html = tpl.replace("__DATA__", json.dumps(bundle, separators=(",", ":")))
    OUTPUT.write_text(html)
    kb = len(html.encode()) / 1024
    print(f"wrote {OUTPUT.relative_to(ROOT)}  ({kb:.0f} KB, {len(bundle['teams'])} teams, "
          f"{bundle['meta']['target_season']}, snapshot {bundle['meta']['snapshot_date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
