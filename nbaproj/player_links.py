"""External link-out: verified basketball-reference.com player page URLs.

The UI wants a "view on Basketball-Reference" link per player. bbref's player-page slugs follow a
well-known formula (last name + first two of first name + a 2-digit disambiguator), but guessing it
risks a silent wrong-player link on any name collision -- a correctness bug, not just a missing
link. Instead this pulls bbref's own **A-Z player index** (`/players/<letter>/`), which lists every
NBA/ABA/BAA player ever with his exact slug and active-years range, so a lookup is a verified exact
match, not a guess. ~26 pages total, cached to disk after the first pull, same politeness as
`nbaproj/odds.py` (the sibling scraper already vetted for this site): browser User-Agent,
`Crawl-delay: 3`. `/players/` is not disallowed by bbref's robots.txt (only `/basketball/`,
`*/gamelog/`, `*/splits/`, `*/on-off/`, `*/lineups/`, `*/shooting/` are).

Same-name collisions (rare but real) are resolved by active-years overlap: pick the index entry
whose [year_min, year_max] season range contains the target player-season, falling back to the
most recent entry if no season is given.
"""

from __future__ import annotations

import html
import logging
import re
import string
import time
import unicodedata
from pathlib import Path

import pandas as pd

from .cache import DATA_DIR

log = logging.getLogger(__name__)

INDEX_CACHE = DATA_DIR / "raw" / "bbref_player_index"
CRAWL_DELAY_S = 3
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _normalize(name: str) -> str:
    """ASCII-fold accents, drop periods WITHOUT splitting ("C.J." -> "cj", not "c j"), strip
    generational suffixes our data may carry that bbref's index name does not ("Bobby Portis Jr."
    -> "bobby portis"), then the usual punctuation-strip/collapse."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.lower().replace(".", "")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    words = [w for w in n.split() if w not in _SUFFIXES]
    return " ".join(words)


def _fetch_letter(letter: str, *, refresh: bool = False) -> str:
    import urllib.request

    INDEX_CACHE.mkdir(parents=True, exist_ok=True)
    path = INDEX_CACHE / f"{letter}.html"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8", errors="replace")

    url = f"https://www.basketball-reference.com/players/{letter}/"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    time.sleep(CRAWL_DELAY_S)
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    path.write_text(body, encoding="utf-8")
    log.info("fetched bbref player index %s (%d bytes)", letter, len(body))
    return body


def _parse_letter(raw: str) -> pd.DataFrame:
    """(slug, name, year_min, year_max) rows -- years are SEASON END years, e.g. 2026 = 2025-26."""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
        slug_m = re.search(r'data-append-csv="([^"]+)"', tr)
        name_m = re.search(r'data-stat="player"[^>]*>.*?<a[^>]*>(.*?)</a>', tr, re.S)
        ymin_m = re.search(r'data-stat="year_min"[^>]*>(\d{4})<', tr)
        ymax_m = re.search(r'data-stat="year_max"[^>]*>(\d{4})<', tr)
        if not (slug_m and name_m and ymin_m and ymax_m):
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", name_m.group(1))).strip()
        rows.append({"slug": slug_m.group(1), "name": name,
                     "year_min": int(ymin_m.group(1)), "year_max": int(ymax_m.group(1))})
    return pd.DataFrame(rows)


def build_index(*, refresh: bool = False) -> pd.DataFrame:
    """The full A-Z player index, concatenated. Each letter page is cached independently."""
    frames = []
    for letter in string.ascii_lowercase:
        raw = _fetch_letter(letter, refresh=refresh)
        df = _parse_letter(raw)
        if not df.empty:
            frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["join_key"] = out["name"].map(_normalize)
    log.info("bbref player index: %d players", len(out))
    return out


_INDEX: pd.DataFrame | None = None


def _index() -> pd.DataFrame:
    global _INDEX
    if _INDEX is None:
        _INDEX = build_index()
    return _INDEX


def url_for(name: str, season_start: int | None = None) -> str | None:
    """The verified basketball-reference.com player-page URL for ``name``, or ``None`` if no exact
    name match exists in the index. If multiple players share the name, prefers the one whose
    active-years range contains ``season_start`` (season_start=2025 -> the 2025-26 season, so it
    must fall within [year_min-1, year_max-1]); falls back to the most recently active if no
    ``season_start`` is given or none contains it.
    """
    idx = _index()
    key = _normalize(name)
    matches = idx[idx["join_key"] == key]
    if matches.empty:
        return None
    if len(matches) > 1 and season_start is not None:
        in_range = matches[(matches["year_min"] - 1 <= season_start)
                           & (season_start <= matches["year_max"] - 1)]
        if not in_range.empty:
            matches = in_range
    row = matches.sort_values("year_max", ascending=False).iloc[0]
    return f"https://www.basketball-reference.com/players/{row['slug'][0]}/{row['slug']}.html"
