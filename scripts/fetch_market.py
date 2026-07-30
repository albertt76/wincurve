"""Pull live prediction-market win totals and write them for the app to show beside ours.

Kalshi's KXNBAWINS series prices every team's season win total as a threshold ladder; this
script reconstructs each team's market-implied win distribution (see nbaproj/market_live.py)
and writes data/processed/market_<season>.json for build_snapshots.py to merge in.

Re-runnable through the season -- pass --refresh to re-pull fresh quotes.

    python scripts/fetch_market.py --refresh

Legend for the printed report (acronyms spelled out, per project convention):
  ours   = our model's projected wins (mean of the simulated season)
  market = Kalshi's implied wins (mean, reconstructed from the threshold ladder)
  diff   = ours - market; positive means we are HIGHER on the team than the crowd
  MAE    = mean absolute error, the average size of |diff| ignoring direction
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbaproj.market_live import market_win_table  # noqa: E402

PROC = Path("data/processed")
SEASON_YY = 27          # 2026-27 season, Kalshi codes it "27"
SEASON_KEY = "2026-27"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull fresh quotes instead of using the cached snapshot")
    args = ap.parse_args()

    table = market_win_table(SEASON_YY, refresh=args.refresh)
    if not table:
        print("no market data recovered -- Kalshi returned nothing priceable")
        return 1

    # Attach our own projection for a side-by-side report, if the current bundle exists.
    ours = {}
    cur_path = PROC / "projections_current.json"
    if cur_path.exists():
        cur = json.loads(cur_path.read_text())
        ours = {t["abbr"]: t for t in cur["teams"]}

    print("=" * 66)
    print(f"LIVE MARKET vs wincurve -- {SEASON_KEY} (Kalshi KXNBAWINS)")
    print("=" * 66)
    print("market = crowd's implied wins; ours = our model; diff = ours - market")
    print(f"{'team':>5} {'ours':>6} {'market':>7} {'diff':>7}   market 80% range")
    print("-" * 66)

    diffs = []
    rows = sorted(table.items(),
                  key=lambda kv: -(kv[1].get("mean") or 0))
    for abbr, d in rows:
        mkt = d.get("mean")
        o = ours.get(abbr, {}).get("wins")
        rng = (f"{d['p10']:.0f}-{d['p90']:.0f}"
               if d.get("p10") is not None and d.get("p90") is not None else "  n/a")
        if o is not None and mkt is not None:
            diff = o - mkt
            diffs.append(abs(diff))
            print(f"{abbr:>5} {o:>6.1f} {mkt:>7.1f} {diff:>+7.1f}   {rng}")
        else:
            print(f"{abbr:>5} {'--':>6} {mkt:>7.1f} {'--':>7}   {rng}")

    if diffs:
        mae = sum(diffs) / len(diffs)
        big = sorted(
            ((ours[a]["wins"] - table[a]["mean"], a) for a in table
             if a in ours and table[a].get("mean") is not None),
            key=lambda t: -abs(t[0]))[:6]
        print("-" * 66)
        print(f"mean |ours - market| = {mae:.2f} wins across {len(diffs)} teams")
        print("biggest disagreements (the deliverable -- where to look for a reason):")
        for diff, a in big:
            side = "we are higher" if diff > 0 else "we are lower"
            print(f"    {a}: {diff:+.1f} wins  ({side})")

    payload = {
        "season": SEASON_KEY,
        "source": "kalshi:KXNBAWINS",
        "teams": {
            abbr: {
                "wins": d.get("mean"),
                "median": round(d["median"], 1) if d.get("median") is not None else None,
                "p10": round(d["p10"], 1) if d.get("p10") is not None else None,
                "p90": round(d["p90"], 1) if d.get("p90") is not None else None,
                "n_rungs": d.get("n_rungs"),
            }
            for abbr, d in table.items()
        },
    }
    out = PROC / "market_2026_27.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {out}  ({len(payload['teams'])} teams)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
