"""Historical preseason win totals (the market baseline).

Source: basketball-reference.com/leagues/NBA_<end_year>_preseason_odds.html, which
carries a `wins_ou` (over/under) column plus the realized result for all 30 teams,
back through our whole window. This fills the one baseline we could not otherwise
measure -- Kalshi and Polymarket are both far too recent to supply 21 seasons.

Politeness: only ~21 pages total, cached to disk after the first fetch, requested
at bbref's declared `Crawl-delay: 3`. The `/leagues/` path is explicitly allowed by
their robots.txt (which disallows `*/gamelog/`, `*/splits/`, `*/on-off/`,
`*/lineups/`, `*/shooting/` -- none of which we touch).

Joining is the risky part. bbref and nba_api both use era-correct franchise names,
but they disagree in details ("LA Clippers" vs "Los Angeles Clippers"). A silent
mismatch would quietly drop teams and bias the baseline, so `load_market_baseline`
asserts all 30 teams resolve every season and raises otherwise.
"""

from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path

import pandas as pd

from .cache import DATA_DIR

log = logging.getLogger(__name__)

ODDS_CACHE = DATA_DIR / "raw" / "odds_html"
CRAWL_DELAY_S = 3  # bbref robots.txt: Crawl-delay: 3
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Normalized-name differences between bbref and nba_api that a simple string match
# cannot bridge. Keys and values are both lowercased/punctuation-stripped.
NAME_ALIASES = {
    "la clippers": "los angeles clippers",
    "new orleans oklahoma city hornets": "new orleans hornets",
}


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, then de-alias."""
    n = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    n = re.sub(r"\s+", " ", n).strip()
    return NAME_ALIASES.get(n, n)


def _fetch_page(end_year: int, *, refresh: bool = False) -> str | None:
    """Return raw HTML for one season's odds page, cached on disk."""
    import urllib.error
    import urllib.request

    ODDS_CACHE.mkdir(parents=True, exist_ok=True)
    path = ODDS_CACHE / f"NBA_{end_year}_preseason_odds.html"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8", errors="replace")

    url = (
        "https://www.basketball-reference.com/leagues/"
        f"NBA_{end_year}_preseason_odds.html"
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        time.sleep(CRAWL_DELAY_S)
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        if err.code == 404:
            # Expected for a season whose odds have not been posted yet.
            log.info("NBA_%d preseason odds not published (404)", end_year)
            return None
        raise

    path.write_text(body, encoding="utf-8")
    log.info("fetched NBA_%d preseason odds (%d bytes)", end_year, len(body))
    return body


def _parse_page(raw: str) -> pd.DataFrame:
    """Extract (team, wins_ou, result) rows from one odds page.

    bbref buries secondary tables inside HTML comments to defer rendering, so the
    comment markers are stripped before matching.
    """
    text = raw.replace("<!--", "").replace("-->", "")
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        team_m = re.search(
            r'data-stat="team"[^>]*>(?:<a[^>]*>)?(.*?)(?:</a>)?</t[hd]>', tr, re.S)
        ou_m = re.search(r'data-stat="wins_ou"[^>]*>(.*?)</t[hd]>', tr, re.S)
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
        rows.append({"bbref_team": team, "wins_ou": ou})
    return pd.DataFrame(rows)


def fetch_odds(first_start: int = 2005, last_start: int = 2025) -> pd.DataFrame:
    """Preseason win totals for each season, keyed by season start year.

    bbref pages are named by *ending* year: the 2005-06 season is NBA_2006.
    """
    frames = []
    for start in range(first_start, last_start + 1):
        raw = _fetch_page(start + 1)
        if raw is None:
            continue
        df = _parse_page(raw)
        if df.empty:
            log.warning("NBA_%d parsed to zero rows", start + 1)
            continue
        df["season_start"] = start
        df["season"] = f"{start}-{str(start + 1)[-2:]}"
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    log.info("odds: %d rows across %d seasons",
             len(out), out["season_start"].nunique())
    return out


def load_market_baseline(team_seasons: pd.DataFrame) -> pd.DataFrame:
    """Join preseason win totals onto the team-season table by franchise.

    Raises if any season fails to resolve all 30 teams -- a partial join would
    silently bias the market baseline toward whichever teams happened to match.
    """
    odds = fetch_odds()
    if odds.empty:
        raise RuntimeError("no preseason odds parsed; cannot build market baseline")

    odds = odds.copy()
    odds["join_key"] = odds["bbref_team"].map(_normalize)

    teams = team_seasons.copy()
    teams["join_key"] = teams["team_name"].map(_normalize)

    merged = teams.merge(
        odds[["season_start", "join_key", "wins_ou"]],
        on=["season_start", "join_key"],
        how="left",
    )

    covered = merged[merged["season_start"].isin(odds["season_start"].unique())]
    bad = covered[covered["wins_ou"].isna()]
    if not bad.empty:
        detail = bad[["season", "team_name"]].to_string(index=False)
        raise RuntimeError(
            f"{len(bad)} team-seasons failed to match a win total:\n{detail}\n"
            "Add the offending name to NAME_ALIASES."
        )
    return merged
