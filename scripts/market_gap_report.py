"""Where do we most disagree with the betting/prediction market, and why?

Ranks all 30 teams by (our projected wins − Kalshi market-implied wins) for the upcoming
season, then dumps the ≈Wins drivers for the biggest gaps so each disagreement is explainable.
Strictly downstream: the market never touches the model; this only *compares*.

Legend:
  ≈W = a player's wins-above-replacement value (oW/dW = its offensive/defensive halves)
  avl = projected availability; a low value means we discount an injury-prone player
  flags: AllD/AllNBA = prior honors (display only); D^/Dv = play-by-play RAPM disagrees with our
         box defense (^ we may underrate, v overrate); OUT = known-absence override
  roster mpg = minutes/game the listed roster supplies vs the 240 budget (>>240 = summer bloat)

    python scripts/market_gap_report.py            # biggest gaps both directions
    python scripts/market_gap_report.py CHA,ATL    # specific teams
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROC = Path("data/processed")


def _cap_factor(team: dict, meta: dict) -> float:
    """Budget-cap factor: the team aggregation scales an over-budget roster down to the
    240-min/game budget before weighting, so ≈Wins must too or it over-credits every player on
    a bloated summer roster. At/under budget it is 1. Mirrors the UI's teamCapFactor()."""
    mpg_budget = meta["minutes_budget"] / meta["full_season_games"]
    load = sum(p["mpg"] * p["avail"] for p in team["players"])
    return mpg_budget / load if load > mpg_budget else 1.0


def _wparts(p: dict, meta: dict, turnover: float, cap: float = 1.0) -> tuple[float, float]:
    """A player's offensive and defensive ≈Wins, mirroring the UI's winParts()."""
    minshare = p["mpg"] * p["avail"] / (meta["minutes_budget"] / meta["full_season_games"]) * cap
    wpp = meta["wins_per_rating_point"]
    ow = wpp * minshare * meta["off_slope"] * (p["off"] - meta["replacement_off"])
    dev = p["def"] - meta["replacement_def"]
    rdr = meta.get("replacement_def_rapm")
    if rdr is not None and "defr" in p:
        w = min(max(turnover, 0.0), 1.0)
        dev = (1 - w) * (p["def"] - meta["replacement_def"]) + w * (p.get("defr", p["def"]) - rdr)
    dw = wpp * minshare * meta["def_slope"] * dev
    return ow, dw


def _flags(p: dict) -> str:
    fl = []
    if p.get("all_def"):
        fl.append(f"AllD{p['all_def'].get('team', '')}")
    if p.get("all_nba"):
        fl.append(f"AllNBA{p['all_nba'].get('team', '')}")
    if "rapm_def" in p and "box_def" in p and abs(p["rapm_def"] - p["box_def"]) >= 1:
        fl.append("D^" if p["rapm_def"] > p["box_def"] else "Dv")
    if p.get("override"):
        fl.append("OUT")
    if p["avail"] < 0.6:
        fl.append(f"avl{p['avail'] * 100:.0f}%")
    return " ".join(fl)


def main() -> int:
    proj = json.load(open(PROC / "projections_current.json"))
    mkt = json.load(open(PROC / "market_2026_27.json"))
    meta, M = proj["meta"], mkt["teams"]
    rows = [(t["abbr"], t, M[t["abbr"]]["wins"], t["wins"] - M[t["abbr"]]["wins"])
            for t in proj["teams"] if t["abbr"] in M]
    rows.sort(key=lambda r: -r[3])

    print("=" * 72)
    print(f"OURS vs MARKET ({mkt.get('source')}) — {proj['meta']['season']}")
    print("=" * 72)
    print(f"{'team':<5}{'ours':>6}{'mkt':>6}{'gap':>7}   {'O':>5}{'D':>5}{'turn':>6}{'rosMpg':>8}")
    for ab, t, mw, gap in rows:
        rmpg = sum(p["mpg"] * p["avail"] for p in t["players"])
        print(f"{ab:<5}{t['wins']:>6.1f}{mw:>6.1f}{gap:>+7.1f}   {t['off_rating']:>+5.1f}"
              f"{t['def_rating']:>+5.1f}{t['turnover'] * 100:>5.0f}%{rmpg:>7.0f}")

    if len(sys.argv) > 1:
        teams = sys.argv[1].split(",")
    else:
        teams = [r[0] for r in rows[:3]] + [r[0] for r in rows[-3:]]
    byab = {t["abbr"]: t for t in proj["teams"]}
    for ab in teams:
        t, mk = byab[ab], M[ab]
        rmpg = sum(p["mpg"] * p["avail"] for p in t["players"])
        print(f"\n{'-' * 72}\n{ab} {t['name']}: OURS {t['wins']:.1f}  MKT {mk['wins']:.1f}  "
              f"GAP {t['wins'] - mk['wins']:+.1f}   (O {t['off_rating']:+.1f} D {t['def_rating']:+.1f}, "
              f"turnover {t['turnover'] * 100:.0f}%, roster {rmpg:.0f} mpg / 240)")
        print(f"  {'player':<22}{'age':>4}{'mpg':>5}{'avl':>5}{'off':>6}{'def':>6}"
              f"{'oW':>6}{'dW':>6}{'≈W':>6}  flags")
        cap = _cap_factor(t, meta)
        scored = [(p, *_wparts(p, meta, t["turnover"], cap)) for p in t["players"]]
        for p, ow, dw in sorted(scored, key=lambda x: -(x[1] + x[2]))[:9]:
            print(f"  {p['name']:<22}{(p.get('age') or '-'):>4}{p['mpg']:>5.1f}{p['avail'] * 100:>4.0f}%"
                  f"{p['off']:>+6.2f}{p['def']:>+6.2f}{ow:>+6.1f}{dw:>+6.1f}{ow + dw:>+6.1f}  {_flags(p)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
