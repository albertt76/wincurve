"""Build the NHL impact viewer: a self-contained web leaderboard of the Stage 2 metrics.

Computes, for every season whose shift chart is pulled (``data/nhl/processed/shifts_<yr>.parquet``):
  - skater **xG-RAPM** (``nhl.rapm``): off / def / net per-60 impact, enriched with position + team
  - goalie **GSAx** (``nhl.goalies``): goals saved above expected, total and per 60

The per-season xG-RAPM fit is expensive (ridge over ~900 skaters x thousands of stints), so each
season's result is cached to ``data/nhl/processed/impact_<yr>.parquet`` and reused unless --refresh.
The leaderboards are inlined into ``ui/nhl/template.html`` (a ``__DATA__`` placeholder) -> a single
self-contained ``ui/nhl/impact.html`` with no external dependencies, mirroring the NBA ``ui/build.py``
convention. Re-run it as more seasons land from the shift pull; it auto-detects available seasons.

    python scripts/nhl_build_impact_ui.py            # build for all available seasons (cached)
    python scripts/nhl_build_impact_ui.py --refresh  # recompute every season's RAPM from scratch
    python scripts/nhl_build_impact_ui.py --alpha 3000 --skater-floor 400 --goalie-floor 500
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nhl import goalies, player_links, rapm  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "ui" / "nhl" / "template.html"
OUTPUT = ROOT / "ui" / "nhl" / "impact.html"


def available_seasons() -> list[int]:
    """Season start-years that have a pulled shift chart, ascending."""
    years = [int(p.stem.split("_")[1]) for p in sorted(PROC.glob("shifts_*.parquet"))]
    return sorted(years)


def _skater_meta(start_year: int) -> pd.DataFrame:
    """player_id -> name / position / primary team, from MoneyPuck's 'all' skater rows.

    A player traded mid-season can appear on multiple teams; we keep the team he played the
    most games for (his primary team that season) so the leaderboard shows one clean tag.
    """
    sk = pd.read_parquet(PROC / "moneypuck_skaters.parquet")
    sk = sk[(sk["season_start"] == start_year) & (sk["situation"] == "all")].copy()
    sk = sk.sort_values("games_played", ascending=False).drop_duplicates("playerId")
    sk["playerId"] = sk["playerId"].astype(int)
    sk["team"] = sk["team"].replace(rapm.MP_CODE_ALIASES)  # dotted legacy codes -> tricodes (display)
    return sk.set_index("playerId")[["name", "position", "team"]]


def _goalie_team(start_year: int) -> dict[int, str]:
    """player_id -> primary team for goalies (most games), same idea as skaters."""
    g = pd.read_parquet(PROC / "moneypuck_goalies.parquet")
    g = g[(g["season_start"] == start_year) & (g["situation"] == "all")].copy()
    g = g.sort_values("games_played", ascending=False).drop_duplicates("playerId")
    g["team"] = g["team"].replace(rapm.MP_CODE_ALIASES)  # dotted legacy codes -> tricodes (display)
    return dict(zip(g["playerId"].astype(int), g["team"]))


def season_skaters(start_year: int, alpha: float, floor_min: int,
                   *, refresh: bool) -> pd.DataFrame:
    """xG-RAPM skater leaderboard for a season (cached), enriched with name/pos/team.

    The cache is keyed on alpha (``impact_<yr>_a<alpha>.parquet``) so a different ridge strength
    never silently reuses another alpha's fit -- mirroring the NBA project's ``rapm_<yr>_a2000``.
    """
    cache = PROC / f"impact_{start_year}_a{int(alpha)}.parquet"
    if cache.exists() and not refresh:
        sk = pd.read_parquet(cache)
    else:
        sk = rapm.season_rapm(start_year, alpha=alpha)
        sk.to_parquet(cache, index=False)
    meta = _skater_meta(start_year)
    sk = sk[sk["toi_min"] * 60 >= floor_min * 60].copy()
    sk["name"] = sk["player_id"].map(meta["name"]).fillna(sk["player_id"].astype(str))
    sk["pos"] = sk["player_id"].map(meta["position"]).fillna("")
    sk["team"] = sk["player_id"].map(meta["team"]).fillna("")
    return sk.sort_values("net", ascending=False).reset_index(drop=True)


def season_goalies(start_year: int, floor_min: int) -> pd.DataFrame:
    """GSAx goalie leaderboard for a season, floored at floor_min total minutes.

    Drops any non-finite gsax_per_60 (a 0-icetime goalie gives 0/0 = NaN, which would become an
    invalid ``NaN`` token in the inlined JSON and break the page); the build also serializes with
    allow_nan=False as a backstop.
    """
    g = goalies.goalie_gsax("all")
    g = g[(g["season_start"] == start_year) & (g["toi_min"] >= floor_min)].copy()
    g = g[np.isfinite(g["gsax_per_60"]) & np.isfinite(g["gsax"])]
    tm = _goalie_team(start_year)
    g["team"] = g["player_id"].map(tm).fillna("")
    return g.sort_values("gsax", ascending=False).reset_index(drop=True)


def build(alpha: float, skater_floor: int, goalie_floor: int, *, refresh: bool) -> dict:
    years = available_seasons()
    if not years:
        raise SystemExit("no shifts_<year>.parquet found -- run scripts/nhl_fetch_shifts.py first")

    seasons: dict[str, dict] = {}
    failed: list[int] = []
    for yr in years:
        print(f"  season {season_str(yr)} ...", flush=True)
        # Isolate each season: one bad season (e.g. a live one not yet in MoneyPuck, or a shift
        # file with no 5v5 stints) must not abort the whole unattended multi-season build.
        try:
            sk = season_skaters(yr, alpha, skater_floor, refresh=refresh)
            gl = season_goalies(yr, goalie_floor)
        except Exception as err:  # noqa: BLE001
            print(f"    SKIP {season_str(yr)}: {type(err).__name__}: {err}", flush=True)
            failed.append(yr)
            continue
        seasons[str(yr)] = {
            "label": season_str(yr),
            # to_dict("records") (not itertuples): the column "def" is a Python keyword, and
            # casting to native float/int keeps json.dumps happy (numpy scalars don't serialize).
            "skaters": [
                {"name": r["name"], "pos": r["pos"] or "", "team": r["team"] or "",
                 "toi": round(float(r["toi_min"])), "off": round(float(r["off"]), 2),
                 "def": round(float(r["def"]), 2), "net": round(float(r["net"]), 2),
                 "hr": player_links.url_for(r["name"], yr)}
                for r in sk.to_dict("records")
            ],
            "goalies": [
                {"name": r["name"], "team": r["team"] or "", "games": int(r["games"]),
                 "toi": round(float(r["toi_min"])), "xga": round(float(r["xga"]), 1),
                 "ga": int(r["ga"]), "gsax": round(float(r["gsax"]), 1),
                 "gsax60": round(float(r["gsax_per_60"]), 2),
                 "hr": player_links.url_for(r["name"], yr)}
                for r in gl.to_dict("records")
            ],
        }
        print(f"    {len(sk)} skaters (>= {skater_floor} min 5v5), "
              f"{len(gl)} goalies (>= {goalie_floor} min)", flush=True)

    if not seasons:
        raise SystemExit(f"no season built successfully (failed: {failed}) -- nothing to write")
    if failed:
        print(f"  (skipped {len(failed)}: {', '.join(season_str(y) for y in failed)})")

    n_players = sum(len(s["skaters"]) + len(s["goalies"]) for s in seasons.values())
    n_linked = sum(sum(p["hr"] is not None for p in s["skaters"] + s["goalies"])
                  for s in seasons.values())
    print(f"  hockey-reference links: {n_linked}/{n_players} player-rows matched")

    ok = sorted(int(k) for k in seasons)  # only seasons that actually built get a pill
    return {
        "meta": {
            "built": dt.date.today().isoformat(),
            "alpha": alpha,
            "skater_floor_min": skater_floor,
            "goalie_floor_min": goalie_floor,
            "season_keys": [str(y) for y in ok],
            "labels": {str(y): season_str(y) for y in ok},
        },
        "seasons": seasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=rapm.DEFAULT_ALPHA)
    rapm_floor = rapm.DEFAULT_MIN_TOI // 60
    ap.add_argument("--skater-floor", type=int, default=rapm_floor,
                    help=f"minimum 5v5 minutes for a skater to appear; can only RAISE above the "
                         f"{rapm_floor}-min RAPM noise floor (lower values are clamped up)")
    ap.add_argument("--goalie-floor", type=int, default=500,
                    help="minimum total minutes for a goalie to appear (default 500)")
    ap.add_argument("--refresh", action="store_true",
                    help="recompute every season's xG-RAPM from stints (ignore the cache)")
    args = ap.parse_args()

    # RAPM is fit at a fixed 400-min noise floor, so a lower display floor cannot surface players
    # that were never estimated -- clamp it up rather than silently ignore a sub-400 request.
    skater_floor = max(args.skater_floor, rapm_floor)
    if skater_floor != args.skater_floor:
        print(f"  note: --skater-floor {args.skater_floor} raised to the {rapm_floor}-min RAPM floor")

    print(f"building NHL impact viewer (alpha={args.alpha:g}, "
          f"skater floor {skater_floor} min, goalie floor {args.goalie_floor} min)")
    data = build(args.alpha, skater_floor, args.goalie_floor, refresh=args.refresh)

    tpl = TEMPLATE.read_text()
    assert "__DATA__" in tpl, "template placeholder __DATA__ missing"
    # allow_nan=False: a stray NaN would serialize as an invalid JSON token and break the page;
    # fail the build loudly instead.
    OUTPUT.write_text(tpl.replace("__DATA__", json.dumps(data, separators=(",", ":"), allow_nan=False)))
    n_seasons = len(data["seasons"])
    print(f"wrote {OUTPUT}  ({OUTPUT.stat().st_size:,} bytes, {n_seasons} season(s): "
          f"{', '.join(data['meta']['labels'].values())})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
