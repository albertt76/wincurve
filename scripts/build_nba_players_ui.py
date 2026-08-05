"""Build the NBA player-impact leaderboard: a self-contained, league-wide web view.

The team-projection UI (``ui/projections.html``) only exposes per-player impact *inside* each
team's expanded panel. This script aggregates every team's ``players`` across each season in
``data/processed/snapshots.json`` -- the same bundle that powers the team UI -- into ONE
sortable / searchable / filterable **league-wide** table per season, and inlines it into
``ui/nba_players/template.html`` (a ``__DATA__`` placeholder) -> a single self-contained
``ui/nba_players/players.html`` with no external dependencies.

It mirrors ``scripts/nhl_build_impact_ui.py`` (auto-detect seasons, native-type casts so
``json.dumps`` is happy, ``allow_nan=False`` as a NaN backstop) and the ``ui/build.py`` inlining
convention. Auto-detects the seasons present in the snapshot -- re-run it whenever the snapshot is
rebuilt (``scripts/build_snapshots.py``).

    python scripts/build_nba_players_ui.py

Each player row also carries a verified ``bbref`` link (``nbaproj.player_links``, an A-Z index of
basketball-reference.com, not a guessed slug) -- the template renders it as a small "↗" that opens
the player's Basketball-Reference page in a new tab.

    python scripts/build_nba_players_ui.py --relink
        Patches bbref links into the ALREADY-BUILT players.html in place, without needing
        snapshots.json -- for when the source snapshot isn't available but the links still are
        (e.g. a fresh checkout that never ran build_snapshots.py). The normal path above already
        includes links every time it runs; this is only for topping up an existing build.

Note on gating: these per-player Off/Def/Impact numbers are premium-gated inside the *team panels*
of the main app (see ``ui/build.py`` / ``ui/api/premium.js``). Per the owner's decision, THIS
standalone leaderboard is published PUBLIC with its data inlined (no serverless gating); the team
panels stay premium. See the PR / root CLAUDE.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbaproj import player_links  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "data" / "processed" / "snapshots.json"
TEMPLATE = ROOT / "ui" / "nba_players" / "template.html"
OUTPUT = ROOT / "ui" / "nba_players" / "players.html"


def add_bbref_links(seasons: dict) -> int:
    """Mutate each player row in ``seasons`` (season_key -> {label, players: [...]}) to add a
    verified ``bbref`` URL (or ``None`` if the name isn't in the index). Works on the flattened
    league-wide list regardless of whether it came from snapshots.json or an existing built page,
    which is what lets ``--relink`` reuse this exact same logic. Returns the number matched."""
    n = matched = 0
    for key, blob in seasons.items():
        season_start = int(key.split("-")[0])
        for p in blob["players"]:
            p["bbref"] = player_links.url_for(p["name"], season_start)
            n += 1
            matched += p["bbref"] is not None
    print(f"  bbref links: {matched}/{n} players matched")
    return matched


def _num(x, ndigits):
    """Native-float round (numpy/None-safe) -- returns None untouched so json.dumps stays valid."""
    if x is None:
        return None
    return round(float(x), ndigits)


def _int(x):
    """Native int, or None (age can be missing for some player-seasons)."""
    return None if x is None else int(x)


def team_cap_factor(team: dict, mpg_budget: float) -> float:
    """Same as ``teamCapFactor`` in ui/template.html: the team's 240-min/game budget cap scales
    an over-budget roster's minutes down before minute-weighting, so ≈Wins must apply the same
    factor or it over-credits every player on a bloated (summer camp invite) roster."""
    load = sum((p.get("mpg") or 0) * (p.get("avail") or 0) for p in team.get("players", []))
    return mpg_budget / load if load > mpg_budget else 1.0


def win_parts(p: dict, team: dict, season_meta: dict, gmeta: dict) -> tuple[float, float, bool]:
    """Port of ``winParts`` in ui/template.html: a player's WAR-like ("wins above replacement")
    value, decomposing the SAME model the team rating uses -- offense and defense priced by their
    own slopes, defense the turnover-weighted blend of box and RAPM. Returns (off, def, split)."""
    mpg_budget = gmeta["minutes_budget"] / gmeta["full_season_games"]
    cap = team_cap_factor(team, mpg_budget)
    minshare = (p.get("mpg") or 0) * (p.get("avail") or 0) / mpg_budget * cap
    off_slope = season_meta.get("off_slope")
    if off_slope is not None:
        def_dev = (p.get("def") or 0) - season_meta["replacement_def"]
        rep_def_rapm = season_meta.get("replacement_def_rapm")
        if rep_def_rapm is not None:
            w = min(max(team.get("turnover") or 0, 0), 1)
            defr = p["defr"] if p.get("defr") is not None else p.get("def")
            def_dev = (1 - w) * ((p.get("def") or 0) - season_meta["replacement_def"]) \
                + w * ((defr or 0) - rep_def_rapm)
        k = gmeta["wins_per_rating_point"] * minshare
        off = k * off_slope * ((p.get("off") or 0) - season_meta["replacement_off"])
        deff = k * season_meta["def_slope"] * def_dev
        return off, deff, True
    total = gmeta["wins_per_rating_point"] * season_meta["rating_slope"] \
        * minshare * ((p.get("impact") or 0) - gmeta["replacement_impact"])
    return 0.0, total, False


def season_players(season_blob: dict, gmeta: dict) -> list[dict]:
    """Flatten every team's roster into one league-wide player list for a season.

    A player can appear on TWO teams' rosters in the same walk-forward snapshot (a mid-season
    trade / roster-reconstruction artifact); the duplicate rows carry identical impact/off/def/mpg
    (verified), so deduping by player id is lossless. We keep the row with a non-empty ``pos`` when
    one exists (cleaner subtitle), else the first seen -- one clean league-wide row per player.
    """
    seen: dict[int, dict] = {}
    for team in season_blob.get("teams", []):
        abbr = team.get("abbr") or ""
        for p in team.get("players", []):
            pid = int(p["id"])
            wins_off, wins_def, split = win_parts(p, team, season_blob, gmeta)
            row = {
                "id": pid,
                "name": str(p.get("name", "")),
                "pos": str(p.get("pos") or ""),
                "team": abbr,
                "impact": _num(p.get("impact"), 2),
                "off": _num(p.get("off"), 2),
                "def": _num(p.get("def"), 2),
                "mpg": _num(p.get("mpg"), 1),
                "age": _int(p.get("age")),
                "wins": _num(wins_off + wins_def, 2),
                "winsOff": _num(wins_off, 2),
                "winsDef": _num(wins_def, 2),
                "winsSplit": split,
            }
            prev = seen.get(pid)
            # prefer the row that actually carries a position (the other is the same player)
            if prev is None or (not prev["pos"] and row["pos"]):
                seen[pid] = row
    return sorted(seen.values(), key=lambda r: (r["impact"] is None, -(r["impact"] or 0)))


def build() -> dict:
    if not SNAPSHOTS.exists():
        raise SystemExit(f"missing {SNAPSHOTS} -- copy it in / run scripts/build_snapshots.py first")
    snap = json.loads(SNAPSHOTS.read_text())
    snapshots = snap["snapshots"]
    gmeta = snap["meta"]

    keys = sorted(snapshots.keys())  # season labels sort correctly ("2017-18" < ... < "2026-27")
    if not keys:
        raise SystemExit("no seasons in snapshots.json -- nothing to write")

    seasons: dict[str, dict] = {}
    teams_union: set[str] = set()
    for k in keys:
        players = season_players(snapshots[k], gmeta)
        seasons[k] = {"label": k, "players": players}
        teams_union.update(p["team"] for p in players if p["team"])
        print(f"  {k}: {len(players)} players "
              f"(top: {players[0]['name']} {players[0]['impact']:+.2f})", flush=True)
    add_bbref_links(seasons)

    return {
        "meta": {
            "built": dt.date.today().isoformat(),
            "season_keys": keys,
            "labels": {k: k for k in keys},          # identity; kept for template symmetry with NHL
            "current": keys[-1],                      # latest = the live upcoming-season projection
            "teams": sorted(teams_union),             # stable team-filter options across all seasons
        },
        "seasons": seasons,
    }


def relink() -> dict:
    """Patch bbref links into the data already inlined in OUTPUT, without needing SNAPSHOTS. Reads
    the __DATA__ blob straight back out of the built page's own <script>."""
    if not OUTPUT.exists():
        raise SystemExit(f"missing {OUTPUT} -- nothing to relink; run a normal build first")
    html = OUTPUT.read_text()
    m = re.search(r"const DATA = (\{.*?\});", html, re.S)
    assert m, "could not find the inlined DATA blob in " + str(OUTPUT)
    data = json.loads(m.group(1))
    add_bbref_links(data["seasons"])
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relink", action="store_true",
                    help="patch bbref links into the existing players.html without snapshots.json")
    args = ap.parse_args()

    if args.relink:
        print(f"relinking bbref URLs into the existing {OUTPUT.name}")
        data = relink()
    else:
        print(f"building NBA player-impact leaderboard from {SNAPSHOTS.name}")
        data = build()

    tpl = TEMPLATE.read_text()
    assert "__DATA__" in tpl, "template placeholder __DATA__ missing"
    # allow_nan=False: a stray NaN/Inf would serialize as an invalid JSON token and break the page;
    # fail the build loudly instead (the source snapshot is already NaN-free, but keep the backstop).
    OUTPUT.write_text(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"), allow_nan=False)))
    n_players = sum(len(s["players"]) for s in data["seasons"].values())
    print(f"wrote {OUTPUT}  ({OUTPUT.stat().st_size:,} bytes, "
          f"{len(data['seasons'])} seasons, {n_players} player-rows, "
          f"{len(data['meta']['teams'])} teams)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
