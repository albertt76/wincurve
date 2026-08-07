"""Build the NBA projections UI: inline the FULL projection bundle into projections.html.

Team detail -- each team's expanded roster / what-if panel (per-player Off/Def and ~Wins, the
play-by-play RAPM defensive read, the disagreement + conviction reasons, the offseason-move
(trade) undo, and the live what-if editor) -- is PUBLIC: each team's `players` array and the
what-if `grid` are inlined directly into the self-contained projections.html, the same way the
standalone player leaderboard (ui/nba_players) already ships its per-player numbers. There is no
password / serverless gate.

History: the detail was briefly split into a public payload + a password-gated serverless payload
(ui/api/premium.js, PREMIUM_PASSWORD); that gate is removed -- the panels now render for everyone.
To re-gate later, restore the public/premium split from git history and re-add the serverless
function.

    python ui/build.py     # after regenerating snapshots.json
"""
from __future__ import annotations
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
data = json.loads((root / "data/processed/snapshots.json").read_text())

# --- inline the FULL payload (teams incl. players + what-if grid) into projections.html ---
tpl = (root / "ui/template.html").read_text()
assert "__DATA__" in tpl, "template placeholder missing"
out = root / "ui/projections.html"
out.write_text(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"))))

nteams = sum(len(s.get("teams", [])) for s in data["snapshots"].values())
print(f"wrote {out}  ({out.stat().st_size:,} bytes, {len(data['snapshots'])} seasons, "
      f"team detail inlined & public)")
