"""Inject the projection bundle into the UI template."""
from __future__ import annotations
import json, sys
from pathlib import Path
root = Path(__file__).resolve().parent.parent
data = json.loads((root / "data/processed/projections_current.json").read_text())
tpl = (root / "ui/template.html").read_text()
assert "__DATA__" in tpl, "template placeholder missing"
out = root / "ui/projections.html"
out.write_text(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
print(f"wrote {out}  ({out.stat().st_size:,} bytes, {len(data['teams'])} teams)")
