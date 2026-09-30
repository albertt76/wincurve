"""Build the NHL "Records" page: the upcoming-season projected standings, self-contained.

Reads the live projection bundle (`data/nhl/processed/projection_current.json`, written by
`scripts/nhl_project_current.py`) -- each team's projected standings points as a mean + calibrated
80% interval, with the offense / defense / carryover drivers -- and inlines it into
`ui/nhl_records/template.html` (a `__DATA__` placeholder) to produce a single dependency-free
`ui/nhl_records/records.html`, mirroring the NBA `ui/build.py` and the NHL impact-viewer build.

Market lines are attached when available, strictly for display beside the projection -- never an
input. Two sources, tried in order (source-aware, like the NBA project's `marketLabel`):

1. **Kalshi implied points** (`nhl.market_live`, `KXNHLSEASONPTS`) -- a live prediction-market
   ladder of season POINTS, our model's exact target unit. The ring is the ladder's median (the
   50/50 point, the same kind of number as a sportsbook O/U line; see the module docstring for why
   not the mean). Preferred: it is re-priced continuously, where the Vegas recap below is a
   one-time snapshot of the opening line.
2. **Vegas points** (`nhl.market_vegas`) -- a sportsbook's season POINTS total, used only for a
   team whose Kalshi ladder is too thinly quoted to read a median from. As of 2026-09 this is
   BetOnline's 2026-07-20 opener via a hand-curated gambling911.com recap (see
   `market_vegas.LIVE_SOURCES` -- it must be re-found and added each season).

    python scripts/nhl_project_current.py       # (re)build the projection bundle first
    python scripts/nhl_build_records_ui.py       # inline it into ui/nhl_records/records.html
    python scripts/nhl_build_records_ui.py --market   # also try to attach live market lines
    python scripts/nhl_build_records_ui.py --market --refresh   # ...re-pulling fresh quotes
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


def attach_kalshi(bundle: dict, *, refresh: bool = False) -> int:
    """Best-effort: attach the live Kalshi implied POINTS median per team. Returns teams matched.

    Teams whose ladder is too thinly quoted to bracket 50% get no Kalshi value here and fall
    through to `attach_vegas`.
    """
    try:
        from nhl import market_live
        yy = (bundle["meta"]["target_start"] + 1) % 100
        table = market_live.market_points_table(yy, refresh=refresh)
        book = f"{market_live.SERIES}, fetched {market_live.fetched_date(yy)}"
    except Exception as err:  # noqa: BLE001 -- market is optional; never block the build
        print(f"kalshi market: skipped ({type(err).__name__}: {err})")
        return 0
    n = 0
    for t in bundle["teams"]:
        m = table.get(t["team"])
        if m and m.get("median") is not None:
            t["mkt"] = m["median"]                 # already points -- plots directly
            t["mkt_source"] = "kalshi"
            t["mkt_book"] = book
            n += 1
    return n


def attach_vegas(bundle: dict, *, refresh: bool = False) -> int:
    """Best-effort: attach the Vegas POINTS total for teams `attach_kalshi` didn't cover.
    Returns teams newly matched."""
    try:
        from nhl import market_vegas
        table = market_vegas.live_points_table(bundle["meta"]["target_start"], refresh=refresh)
    except Exception as err:  # noqa: BLE001 -- market is optional; never block the build
        print(f"vegas market: skipped ({type(err).__name__}: {err})")
        return 0
    n = 0
    for t in bundle["teams"]:
        if t.get("mkt") is not None:
            continue  # Kalshi already covered this team
        m = table.get(t["team"])
        if m:
            t["mkt"] = round(float(m["points_ou"]), 1)   # already points -- plots directly
            t["mkt_source"] = "vegas"
            t["mkt_book"] = m["source"]
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", action="store_true", help="also try to attach live market lines")
    ap.add_argument("--refresh", action="store_true",
                    help="with --market: re-pull fresh quotes instead of the cached snapshot")
    args = ap.parse_args()

    if not BUNDLE.exists():
        print(f"missing {BUNDLE.relative_to(ROOT)} -- run scripts/nhl_project_current.py first")
        return 1
    bundle = json.loads(BUNDLE.read_text())

    if args.market:
        nk = attach_kalshi(bundle, refresh=args.refresh)
        nv = attach_vegas(bundle, refresh=args.refresh)
        print(f"market: {nk} teams from Kalshi, {nv} from Vegas" if nv or nk
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
