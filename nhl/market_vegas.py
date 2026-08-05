"""LIVE current-season Vegas points totals -- fills the gap while Kalshi KXNHLWINS is dormant.

``nhl/market_live.py`` (Kalshi) has zero open events for the 2026-27 season -- win-total markets
there post nearer to opening night. Real Vegas sportsbooks price NHL season **points** totals much
earlier (BetOnline's 2026-27 line opened 7/20/26, per gambling911.com's recap of it), and in
**points**, our model's exact target unit -- no wins->points conversion needed, unlike the Kalshi
ring (see ``scripts/nhl_build_records_ui.py``'s ``attach_market``).

Unlike ``nhl/odds.py`` (hockey-reference.com's clean, parameterized-by-year historical archive),
there is no equally clean *live* source with a stable per-season URL: sportsbook lines live on
odds-aggregator recap pages whose URL is specific to that article, not a predictable pattern. So
this module's source is a **hand-curated, season-keyed URL** (``LIVE_SOURCES``) -- the same
discipline the project already uses for `data/overrides/known_absences.json`: a value that must be
re-found and updated each season, documented rather than silently stale. Verified 2026-08: the page
is a real, static (non-JS-rendered) HTML table -- ``curl`` with a browser User-Agent returns it
directly, no headless browser needed.
"""

from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from pathlib import Path

import pandas as pd

from .ingest import DATA_DIR, PROC

log = logging.getLogger(__name__)

MARKET_CACHE = DATA_DIR / "raw" / "market"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# season_start -> (source URL, sportsbook attribution). MUST be re-found and added each season --
# there is no stable/parameterized URL for a live line the way hockey-reference.com's historical
# archive has. Checked 2026-08: BetOnline via gambling911.com, published 2026-07-20, all 32 teams.
LIVE_SOURCES: dict[int, tuple[str, str]] = {
    2026: ("https://www.gambling911.com/2026-2027-nhl-totals-for-every-team-futures-odds",
          "BetOnline (via gambling911.com, 2026-07-20)"),
}

# hockey-reference/NHL-API team-name normalization also applies here (see nhl.odds._normalize);
# gambling911's table uses plain full names ("Utah Mammoth", "St. Louis Blues") so no extra
# aliasing has been needed yet -- kept separate from nhl.odds's map since the two sources could
# drift independently.
NAME_ALIASES: dict[str, str] = {}


def _normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-z0-9 ]", " ", n.lower())
    n = re.sub(r"\s+", " ", n).strip()
    return NAME_ALIASES.get(n, n)


def _fetch(url: str, *, refresh: bool = False) -> str:
    import urllib.request

    MARKET_CACHE.mkdir(parents=True, exist_ok=True)
    path = MARKET_CACHE / (re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[-120:] + ".html")
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8", errors="replace")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    time.sleep(1)
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    path.write_text(body, encoding="utf-8")
    log.info("fetched live Vegas points page (%d bytes) from %s", len(body), url)
    return body


def _parse_table(raw: str) -> pd.DataFrame:
    """(team, points_ou) from the gambling911-style table: Team | <old date> | <new date> | +/-.

    Takes the LAST numeric column before the +/- delta as the current line (the table's newest
    dated column, per the header's date labels) -- robust to which specific two dates are shown.
    """
    tables = re.findall(r"<table[^>]*>.*?</table>", raw, re.S)
    rows_out = []
    for t in tables:
        rows = re.findall(r"<tr[^>]*>.*?</tr>", t, re.S)
        if len(rows) < 5:
            continue
        for tr in rows[1:]:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(cells) < 3:
                continue
            clean = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
            team = clean[0]
            nums = [c for c in clean[1:] if re.match(r"^-?\d+(\.\d+)?$", c.replace("&nbsp;", ""))]
            if not team or not nums:
                continue
            # the delta column (last) is signed/small; the O/U line is the largest number shown
            ou_candidates = [float(n) for n in clean[1:-1] if re.match(r"^\d+(\.\d+)?$", n)]
            if not ou_candidates:
                continue
            rows_out.append({"src_team": team, "points_ou": ou_candidates[-1]})
        if rows_out:
            break
    return pd.DataFrame(rows_out)


def live_points_table(season_start: int, *, refresh: bool = False) -> dict[str, dict]:
    """Live Vegas points O/U per NHL tricode for ``season_start``, or ``{}`` if no source is
    curated for that season yet (add one to ``LIVE_SOURCES`` -- see the module docstring)."""
    src = LIVE_SOURCES.get(season_start)
    if src is None:
        log.info("no curated live-odds source for season_start=%d", season_start)
        return {}
    url, attribution = src
    raw = _fetch(url, refresh=refresh)
    df = _parse_table(raw)
    if df.empty:
        log.warning("live Vegas points page parsed to zero rows: %s", url)
        return {}

    ref = pd.read_parquet(PROC / "team_reference.parquet")
    ref["join_key"] = ref["team_name"].map(_normalize)
    df["join_key"] = df["src_team"].map(_normalize)
    m = df.merge(ref[["tricode", "join_key"]], on="join_key", how="left")
    unmatched = m[m["tricode"].isna()]
    if not unmatched.empty:
        log.warning("live Vegas points: %d unmatched teams (skipped): %s",
                   len(unmatched), list(unmatched["src_team"]))
    m = m.dropna(subset=["tricode"])
    return {r["tricode"]: {"points_ou": r["points_ou"], "source": attribution}
            for _, r in m.iterrows()}
