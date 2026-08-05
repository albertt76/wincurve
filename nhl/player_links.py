"""External link-out: verified hockey-reference.com player page URLs.

The NHL analog of ``nbaproj/player_links.py`` -- same rationale (a guessed slug risks a silent
wrong-player link on any name collision), same fix (pull the site's own A-Z player index,
``/players/<letter>/``, which lists every NHL player ever with his exact slug and active-years
range), same politeness (browser User-Agent, ``Crawl-delay: 3``, ~26 pages cached to disk). Verified
2026-08: hockey-reference marks each index entry ``class="nhl"`` or ``class="non_nhl"`` (players who
only played in another league on the same site) -- only ``nhl`` entries are kept, so a same-name
minor-leaguer can never be mistaken for the NHL player.
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

from .ingest import DATA_DIR

log = logging.getLogger(__name__)

INDEX_CACHE = DATA_DIR / "raw" / "hr_player_index"
CRAWL_DELAY_S = 3
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _normalize(name: str) -> str:
    """ASCII-fold accents, drop periods WITHOUT splitting ("T.J." -> "tj", not "t j"), strip
    generational suffixes our data may carry that hockey-reference's index name does not, then the
    usual punctuation-strip/collapse."""
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

    url = f"https://www.hockey-reference.com/players/{letter}/"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    time.sleep(CRAWL_DELAY_S)
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    path.write_text(body, encoding="utf-8")
    log.info("fetched hockey-reference player index %s (%d bytes)", letter, len(body))
    return body


def _parse_letter(raw: str) -> pd.DataFrame:
    """(slug, name, year_min, year_max, pos) for NHL-only entries. Years are SEASON END years,
    e.g. 2026 = 2025-26. ``non_nhl`` entries (minor/other-league players on the same index) are
    dropped so a same-name non-NHL player can never be mistaken for the NHL one."""
    rows = []
    for p in re.findall(r'<p class="(nhl|non_nhl)">(.*?)</p>', raw, re.S):
        cls, body = p
        if cls != "nhl":
            continue
        # active players are wrapped in <strong>...</strong>, so allow ANY markup (not just
        # whitespace) between </a> and the years-parenthesis -- not just \s*.
        m = re.search(r'<a href="/players/[a-z]/([a-z0-9]+)\.html">(.*?)</a>.*?'
                      r"\((\d{4})-(\d{4}),\s*([^)]*)\)", body, re.S)
        if not m:
            continue
        slug, name_raw, ymin, ymax, pos = m.groups()
        rows.append({"slug": slug, "name": html.unescape(name_raw).strip(),
                     "year_min": int(ymin), "year_max": int(ymax), "pos": pos.strip()})
    return pd.DataFrame(rows)


def build_index(*, refresh: bool = False) -> pd.DataFrame:
    frames = []
    for letter in string.ascii_lowercase:
        raw = _fetch_letter(letter, refresh=refresh)
        df = _parse_letter(raw)
        if not df.empty:
            frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["join_key"] = out["name"].map(_normalize)
    log.info("hockey-reference player index: %d NHL players", len(out))
    return out


_INDEX: pd.DataFrame | None = None


def _index() -> pd.DataFrame:
    global _INDEX
    if _INDEX is None:
        _INDEX = build_index()
    return _INDEX


def url_for(name: str, season_start: int | None = None) -> str | None:
    """The verified hockey-reference.com player-page URL for ``name``, or ``None`` if no exact
    name match exists among NHL players. Disambiguates a same-name collision by active-years
    overlap with ``season_start`` (2025 -> the 2025-26 season), falling back to most-recent."""
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
    return f"https://www.hockey-reference.com/players/{row['slug'][0]}/{row['slug']}.html"
