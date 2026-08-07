"""Build the NBA UI pages: inline data into the self-contained HTML shipped to Vercel.

Two pages are emitted from data/processed/snapshots.json:

1. ui/projections.html  (route /)  -- the Records page. Team detail -- each team's expanded
   roster / what-if panel (per-player Off/Def and ~Wins, the play-by-play RAPM defensive read, the
   disagreement + conviction reasons, the offseason-move (trade) undo, and the live what-if editor)
   -- is PUBLIC: each team's `players` array and the what-if `grid` are inlined directly, the same
   way the standalone player leaderboard (ui/nba_players) already ships its per-player numbers.
   There is no password / serverless gate.

2. ui/performance/performance.html  (route /performance)  -- the Performance page: the Track record
   (model vs market vs actual) and Drift (projected wins over logged runs) views that used to be
   tabs on the Records page. It is intentionally UNLINKED from every nav bar -- reachable by direct
   URL only -- so it stays off the main NBA page but remains accessible. It only needs a SLIM slice
   of the snapshots (per-team wins/actual/mkt_wins + the projection history), so the heavy per-team
   `players`/`grid` payload is stripped before inlining to keep the page light.

History: the Records detail was briefly split into a public payload + a password-gated serverless
payload (ui/api/premium.js, PREMIUM_PASSWORD); that gate is removed. To re-gate later, restore the
public/premium split from git history and re-add the serverless function.

    python ui/build.py     # after regenerating snapshots.json
"""
from __future__ import annotations
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
data = json.loads((root / "data/processed/snapshots.json").read_text())


def inline(template: Path, out: Path, payload) -> None:
    tpl = template.read_text()
    assert "__DATA__" in tpl, f"template placeholder missing in {template}"
    out.write_text(tpl.replace("__DATA__", json.dumps(payload, separators=(",", ":"))))


# --- 1) Records page: inline the FULL payload (teams incl. players + what-if grid) ---
proj_out = root / "ui/projections.html"
inline(root / "ui/template.html", proj_out, data)
nteams = sum(len(s.get("teams", [])) for s in data["snapshots"].values())
print(f"wrote {proj_out}  ({proj_out.stat().st_size:,} bytes, {len(data['snapshots'])} seasons, "
      f"team detail inlined & public)")

# --- 2) Performance page: inline a SLIM payload (only what Track record + Drift read) ---
# Track record needs, per completed season, each team's projected wins, actual result, and market
# line; Drift needs the projection history. The per-team `players`/`grid` blobs are NOT needed here,
# so they are dropped -- this keeps the performance page small (it does not carry the roster editor).
slim = {
    "meta": {"seasons": data["meta"]["seasons"]},
    "projection_history": data.get("projection_history", []),
    "snapshots": {
        k: {
            "season": s["season"],
            "season_start": s["season_start"],
            "is_current": s["is_current"],
            "teams": [
                {"abbr": t["abbr"], "wins": t.get("wins"),
                 "actual": t.get("actual"), "mkt_wins": t.get("mkt_wins")}
                for t in s["teams"]
            ],
        }
        for k, s in data["snapshots"].items()
    },
}
perf_out = root / "ui/performance/performance.html"
inline(root / "ui/performance/template.html", perf_out, slim)
print(f"wrote {perf_out}  ({perf_out.stat().st_size:,} bytes, "
      f"{len(slim['projection_history'])} logged runs, slim payload, UNLINKED /performance)")
