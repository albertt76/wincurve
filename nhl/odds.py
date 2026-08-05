"""Historical preseason POINTS totals (the Vegas market baseline) -- the NHL analog of
``nbaproj/odds.py``, same source family, same scraping discipline.

Source: hockey-reference.com/leagues/NHL_<end_year>_preseason_odds.html, which carries an
``over_under`` (points, not wins -- matches our own modeling target exactly, no wins->points
conversion needed) column plus the realized result, for every season since 2005-06 (the NHL's
first season after the 2004-05 lockout cancellation -- matches our own data floor,
``nhl.ingest.FIRST_SUMMARY_SEASON``). This fills the historical baseline Kalshi cannot: its
``KXNHLWINS`` series has zero settled events (see ``nhl/market_live.py``), so it carries no history
at all, and this had been recorded as a blocked item in DESIGN.md until this source was found.

Politeness: ~21 pages total, cached to disk after the first fetch, requested at hockey-reference's
declared ``Crawl-delay: 3``. The ``/leagues/`` path is not disallowed by their robots.txt (only
``/hockey/``, ``*/gamelog/``, ``*/splits/`` and a handful of others are -- none of which we touch),
mirroring exactly how the NBA project already treats basketball-reference.com, the sibling site
under the same network (Sports Reference LLC) with the same robots.txt shape and the same
``data-stat`` table-cell convention.

Joining is the risky part, same as the NBA project: hockey-reference and our own team_summary use
era-correct franchise names, but formatting can differ. ``nhl.teams.target_table()`` already
carries the era-correct ``team_name`` per (team, season_start), so the join key is
(season_start, normalized name); ``load_market_baseline`` asserts every team resolves each season
and raises otherwise, rather than silently dropping a team.
"""

from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from pathlib import Path

import pandas as pd

from .ingest import DATA_DIR, FIRST_SUMMARY_SEASON, LAST_SEASON

log = logging.getLogger(__name__)

ODDS_CACHE = DATA_DIR / "raw" / "odds_html"
CRAWL_DELAY_S = 3  # hockey-reference robots.txt: Crawl-delay: 3
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Normalized-name differences between hockey-reference and the NHL API that a simple
# lowercase/punctuation-strip cannot bridge. Keys and values are both already normalized.
NAME_ALIASES: dict[str, str] = {}


def _normalize(name: str) -> str:
    """ASCII-fold accents (the NHL API's "Montréal" vs hockey-reference's "Montreal"),
    lowercase, strip remaining punctuation, collapse whitespace, then de-alias."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-z0-9 ]", " ", n.lower())
    n = re.sub(r"\s+", " ", n).strip()
    return NAME_ALIASES.get(n, n)


def _fetch_page(end_year: int, *, refresh: bool = False) -> str | None:
    """Return raw HTML for one season's preseason-odds page, cached on disk."""
    import urllib.error
    import urllib.request

    ODDS_CACHE.mkdir(parents=True, exist_ok=True)
    path = ODDS_CACHE / f"NHL_{end_year}_preseason_odds.html"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8", errors="replace")

    url = f"https://www.hockey-reference.com/leagues/NHL_{end_year}_preseason_odds.html"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        time.sleep(CRAWL_DELAY_S)
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        if err.code == 404:
            log.info("NHL_%d preseason odds not published (404)", end_year)
            return None
        raise

    path.write_text(body, encoding="utf-8")
    log.info("fetched NHL_%d preseason odds (%d bytes)", end_year, len(body))
    return body


def _parse_page(raw: str) -> pd.DataFrame:
    """Extract (team, points_ou, final_points) rows from one odds page.

    hockey-reference tags each cell with ``data-stat`` -- ``team_name`` / ``over_under`` /
    ``points`` (the ``points`` cell's ``csk`` sort-key attribute carries the clean final points
    int, sidestepping its display text's " (o)"/" (u)" suffix). An UNPOSTED season (the current
    one, before books open) parses to zero rows -- the caller treats that as "not yet available",
    matching the NBA project's 404 handling for the same not-posted-yet situation.
    """
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
        team_m = re.search(r'data-stat="team_name"[^>]*>(?:<a[^>]*>)?(.*?)(?:</a>)?</t[hd]>',
                           tr, re.S)
        ou_m = re.search(r'data-stat="over_under"[^>]*>(.*?)</t[hd]>', tr, re.S)
        pts_m = re.search(r'data-stat="points"[^>]*?csk="(-?\d+)"', tr)
        if not (team_m and ou_m):
            continue

        def clean(m: re.Match) -> str:
            return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()

        team = clean(team_m)
        ou_raw = clean(ou_m)
        if not team or team.lower() == "team" or not ou_raw:
            continue
        try:
            ou = float(ou_raw)
        except ValueError:
            continue
        rows.append({"hr_team": team, "points_ou": ou,
                     "final_points": int(pts_m.group(1)) if pts_m else None})
    return pd.DataFrame(rows)


def fetch_odds(first_start: int = FIRST_SUMMARY_SEASON, last_start: int = LAST_SEASON
              ) -> pd.DataFrame:
    """Preseason points totals for each season, keyed by season start year.

    hockey-reference pages are named by *ending* year: the 2005-06 season is NHL_2006.
    """
    frames = []
    for start in range(first_start, last_start + 1):
        raw = _fetch_page(start + 1)
        if raw is None:
            continue
        df = _parse_page(raw)
        if df.empty:
            log.info("NHL_%d parsed to zero rows (not yet posted)", start + 1)
            continue
        df["season_start"] = start
        df["season"] = f"{start}-{str(start + 1)[-2:]}"
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    log.info("odds: %d rows across %d seasons", len(out), out["season_start"].nunique())
    return out


def load_market_baseline(target: pd.DataFrame) -> pd.DataFrame:
    """Join preseason points totals onto ``nhl.teams.target_table()``'s output by franchise.

    Raises if any season with a posted odds page fails to resolve every team it should -- a
    partial join would silently bias the baseline toward whichever teams happened to match.
    """
    odds = fetch_odds()
    if odds.empty:
        raise RuntimeError("no preseason odds parsed; cannot build market baseline")

    odds = odds.copy()
    odds["join_key"] = odds["hr_team"].map(_normalize)

    teams = target.copy()
    teams["join_key"] = teams["team_name"].map(_normalize)

    merged = teams.merge(
        odds[["season_start", "join_key", "points_ou", "final_points"]],
        on=["season_start", "join_key"], how="left")

    covered = merged[merged["season_start"].isin(odds["season_start"].unique())]
    bad = covered[covered["points_ou"].isna()]
    if not bad.empty:
        detail = bad[["season", "team_name"]].to_string(index=False)
        raise RuntimeError(
            f"{len(bad)} team-seasons failed to match a points total:\n{detail}\n"
            "Add the offending name to NAME_ALIASES.")
    return merged
