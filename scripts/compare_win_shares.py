"""Compare our player valuation (≈Wins) against Basketball-Reference **Win Shares** (WS).

Win Shares (WS) is bbref's estimate of how many of a team's wins a player produced; it splits
into Offensive (OWS) and Defensive (DWS) halves. Our ≈Wins is the WAR-like (wins-above-
replacement) translation of our impact metric. This script pulls WS for a completed season from
bbref's ``/leagues/`` advanced page (allowed by their robots.txt, honoring ``Crawl-delay: 3``,
like ``nbaproj/odds.py``), recomputes our ≈Wins on the SAME season's actual impact + minutes, and
reports where the two most disagree.

Legend for the printout:
  WS   = Win Shares (bbref total wins produced)      OWS/DWS = its offense / defense halves
  ≈W   = our wins-above-replacement value            oW/dW   = its offense / defense halves
  gap  = WS − ≈W  (positive = bbref credits more)     r       = correlation (0..1, higher = agree)

**Finding (2025-26):** overall r=0.84, but **offense r=0.89 vs defense r=0.52** — the two metrics
agree on offense and diverge on defense, because bbref's DWS is essentially *team* defense
allocated by minutes, while ours is individual. So bbref over-credits perimeter role players on
good-defense teams (Brunson's gap is +7.3, almost all defensive: bbref +2.2 vs our −4.3) and
under-credits players whose value is their own defense that our tracking features now catch
(Dyson Daniels rates +1.8 for us). A uniform ~+1.8-win baseline offset (WS counts from zero, ≈W
from replacement) sits under every gap. Not a projection input -- a valuation cross-check.

    python scripts/compare_win_shares.py [end_year]     # default: last completed season
"""

from __future__ import annotations

import html as htmlmod
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nbaproj.ingest import LAST_COMPLETE_SEASON  # noqa: E402
from nbaproj.odds import CRAWL_DELAY_S, USER_AGENT  # noqa: E402

PROC = Path("data/processed")
CACHE = PROC.parent / "raw"


def _norm(name: str) -> str:
    """Diacritic-insensitive join key (bbref 'Nikola Jokić' <-> nba_api 'Nikola Jokic')."""
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", ascii_name.lower())


def fetch_win_shares(end_year: int, *, refresh: bool = False) -> pd.DataFrame:
    """Win Shares from bbref's advanced page for the season ending in `end_year` (2025-26 =
    2026). Cached under data/raw/. One row per player (the season-total row for traded players)."""
    cache = CACHE / f"bbref_advanced_{end_year}.html"
    if refresh or not cache.exists():
        url = f"https://www.basketball-reference.com/leagues/NBA_{end_year}_advanced.html"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        time.sleep(CRAWL_DELAY_S)
        with urllib.request.urlopen(req, timeout=45) as resp:
            CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_text(resp.read().decode("utf-8", "replace"))
    html = cache.read_text()

    # Anchor each row on its player id (data-append-csv) and read to </tr>; robust to bbref
    # repeating the table across widgets (a plain <tr>..</tr> split silently drops half of them).
    rows = []
    for m in re.finditer(r'data-append-csv="([^"]+)"(.*?)</tr>', html, re.S):
        pid, seg = m.group(1), m.group(2)
        if 'data-stat="ws"' not in seg:
            continue
        rec = {"bbref_id": pid}
        for key, val in re.findall(r'data-stat="([^"]+)"[^>]*?>(.*?)</t[dh]>', seg, re.S):
            rec[key] = htmlmod.unescape(re.sub(r"<[^>]+>", "", val)).strip()
        rows.append(rec)
    ws = pd.DataFrame(rows).rename(columns={"name_display": "player", "team_name_abbr": "tm"})
    for c in ["ws", "ows", "dws", "mp"]:
        ws[c] = pd.to_numeric(ws.get(c), errors="coerce")
    ws = ws.dropna(subset=["ws"]).reset_index(drop=True)
    # traded player: keep the season-total row (max WS = sum of the stints)
    ws = ws.loc[ws.groupby("bbref_id")["ws"].idxmax()].copy()
    ws["key"] = ws["player"].map(_norm)
    return ws[["player", "tm", "mp", "ws", "ows", "dws", "key"]]


def our_approx_wins(season_start: int) -> pd.DataFrame:
    """Our ≈Wins for `season_start` (actual impact + minutes), split into offense/defense.

    Uses the shipped calibration slopes (projections_current.json meta) -- stable across folds --
    so it mirrors the UI's ≈Wins formula. A qualitative player-level comparison, not a backtest.
    """
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    imp = imp[imp["season_start"] == season_start].copy()
    m = json.load(open(PROC / "projections_current.json"))["meta"]
    # minshare = a player's season minutes as a share of the 240-per-game budget over 82 games,
    # matching the UI's winParts(): minshare = mpg*avail/240 accumulated = total_min/(240*82).
    # minutes_budget already equals 240*82 = 19680.
    minshare = imp["minutes"] / m["minutes_budget"]
    wpp = m["wins_per_rating_point"]
    imp["oW"] = wpp * minshare * m["off_slope"] * (imp["off_impact"] - m["replacement_off"])
    imp["dW"] = wpp * minshare * m["def_slope"] * (imp["def_impact"] - m["replacement_def"])
    imp["approx_win"] = imp["oW"] + imp["dW"]
    imp["key"] = imp["player_name"].map(_norm)
    return imp[["player_name", "key", "minutes", "off_impact", "def_impact", "oW", "dW",
                "approx_win"]]


def main() -> int:
    end_year = int(sys.argv[1]) if len(sys.argv) > 1 else LAST_COMPLETE_SEASON + 1
    season_start = end_year - 1
    label = f"{season_start}-{str(end_year)[-2:]}"
    ws = fetch_win_shares(end_year)
    ours = our_approx_wins(season_start)
    j = ws.merge(ours, on="key", how="inner")
    j = j[j["mp"] >= 500].copy()                      # rotation players only
    j["gap"] = j["ws"] - j["approx_win"]
    j["off_gap"] = j["ows"] - j["oW"]
    j["def_gap"] = j["dws"] - j["dW"]

    print("=" * 78)
    print(f"WIN SHARES (bbref) vs our ≈Wins  —  {label}  ({len(j)} rotation players, MP>=500)")
    print("=" * 78)
    print("  WS = Win Shares (bbref total wins produced)    OWS/DWS = its offense/defense halves")
    print("  ≈W = our wins-above-replacement value          oW/dW   = its offense/defense halves")
    print("  gap = WS − ≈W (positive = bbref credits more)  r = correlation (higher = agree)")
    print(f"\nWS sum={ws['ws'].sum():.0f} (~league wins) | our ≈Wins sum={ours['approx_win'].sum():.0f} "
          f"(above replacement, so lower)")
    print(f"correlation  overall r={j['ws'].corr(j['approx_win']):.2f}   "
          f"offense r={j['ows'].corr(j['oW']):.2f}   defense r={j['dws'].corr(j['dW']):.2f}")
    print(f"mean(WS − ≈W) = {j['gap'].mean():+.2f}  (uniform baseline offset)")

    print(f"\nTOP 12 disagreements |WS − ≈W|:")
    print(f"  {'player':<24}{'WS':>5}{'OWS':>5}{'DWS':>5} | {'≈W':>6}{'oW':>6}{'dW':>6} | "
          f"{'gap':>6}{'off':>6}{'def':>6}")
    top = j.reindex(j["gap"].abs().sort_values(ascending=False).index).head(12)
    for _, r in top.iterrows():
        print(f"  {r['player']:<24}{r['ws']:>5.1f}{r['ows']:>5.1f}{r['dws']:>5.1f} | "
              f"{r['approx_win']:>6.1f}{r['oW']:>6.1f}{r['dW']:>6.1f} | "
              f"{r['gap']:>+6.1f}{r['off_gap']:>+6.1f}{r['def_gap']:>+6.1f}")

    print(f"\nWhere WE rate higher (elite individual defenders / efficient scorers):")
    for _, r in j.reindex((j["approx_win"] - j["ws"]).sort_values(ascending=False).index).head(6).iterrows():
        print(f"  {r['player']:<24} WS {r['ws']:>4.1f} (dws {r['dws']:>4.1f})  "
              f"≈W {r['approx_win']:>5.1f} (dW {r['dW']:>5.1f})  gap {r['approx_win'] - r['ws']:+.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
