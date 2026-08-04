"""Disk-backed, throttled, retrying HTTP client.

The NHL data layer (like the NBA one) makes hundreds to thousands of calls across a
full historical pull: team summaries, rosters, per-game play-by-play, shift charts,
and MoneyPuck CSVs. Every call goes through here so re-runs are free and idempotent,
which is what makes the walk-forward backtest practical to iterate on.

This is the NBA project's ``nbaproj/cache.py`` pattern generalized to arbitrary HTTP
endpoints (JSON and CSV/text), since NHL sources are plain REST/CSV rather than the
``nba_api`` client. It is the first piece extracted into the shared ``core`` package.

Design notes:
- Responses are cached to disk keyed by a hash of the full URL + query params. An
  empty/absent result is NOT cached (unlike the nba_api wrapper): NHL endpoints
  return real data or an error, and a transient 5xx should be retried on the next
  run rather than frozen as "no data".
- Throttling is per-``HttpCache`` instance with jitter, to stay under each host's
  informal rate limit without a lockstep pattern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    # NHL and MoneyPuck both reject or throttle the default python-requests UA.
    "User-Agent": "Mozilla/5.0 (compatible; wincurve-multisport/0.1; research)",
    "Accept": "application/json, text/csv, */*",
}


class HttpCache:
    """A throttled, retrying GET client that memoizes responses to ``cache_dir``.

    One instance per data source (its own throttle clock and cache folder):

        HTTP = HttpCache(Path("data/nhl/raw"), min_interval=0.3)
        standings = HTTP.get_json("https://api-web.nhle.com/v1/standings/now")
        skaters = HTTP.get_text("https://moneypuck.com/.../skaters.csv")
    """

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        min_interval: float = 0.3,
        max_attempts: int = 5,
        headers: dict | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.headers = headers or DEFAULT_HEADERS
        self._last_call_at = 0.0
        self._session = requests.Session()

    # -- internals -----------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.15))
        self._last_call_at = time.monotonic()

    def _path(self, url: str, params: dict | None, ext: str) -> Path:
        blob = url + "?" + urlencode(sorted((params or {}).items()))
        digest = hashlib.sha1(blob.encode()).hexdigest()[:16]
        # A readable prefix (last URL path segment) aids eyeballing the cache folder.
        stem = url.rstrip("/").rsplit("/", 1)[-1][:40].replace("?", "_") or "root"
        return self.cache_dir / f"{stem}__{digest}.{ext}"

    def _fetch(self, url: str, params: dict | None) -> requests.Response:
        last_err: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self._throttle()
                resp = self._session.get(
                    url, params=params, headers=self.headers, timeout=30
                )
                resp.raise_for_status()
                return resp
            except Exception as err:  # noqa: BLE001 - transport errors vary
                last_err = err
                backoff = min(2**attempt, 30) + random.uniform(0, 1)
                log.warning(
                    "GET %s attempt %d/%d failed (%s); retry in %.1fs",
                    url, attempt, self.max_attempts, type(err).__name__, backoff,
                )
                time.sleep(backoff)
        raise RuntimeError(f"GET {url} failed after {self.max_attempts} attempts") from last_err

    # -- public API ----------------------------------------------------------
    def get_json(self, url: str, params: dict | None = None, *, refresh: bool = False):
        """Return parsed JSON for ``url`` (+params), from disk if present."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(url, params, "json")
        if path.exists() and not refresh:
            return json.loads(path.read_text())
        data = self._fetch(url, params).json()
        path.write_text(json.dumps(data))
        log.info("fetched json %s %s", url, params or "")
        return data

    def get_bytes(self, url: str, params: dict | None = None, *, refresh: bool = False) -> bytes:
        """Return the raw response body for ``url`` (+params), from disk if present.

        Used for binary downloads (MoneyPuck's zipped shot files); unzip at the call
        site with ``zipfile.ZipFile(io.BytesIO(...))``.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(url, params, "bin")
        if path.exists() and not refresh:
            return path.read_bytes()
        content = self._fetch(url, params).content
        path.write_bytes(content)
        log.info("fetched bytes %s %s", url, params or "")
        return content

    def get_text(self, url: str, params: dict | None = None, *, refresh: bool = False) -> str:
        """Return the raw text body for ``url`` (+params), from disk if present.

        Used for CSV endpoints (MoneyPuck); parse the returned string with
        ``pandas.read_csv(io.StringIO(text))`` at the call site.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(url, params, "txt")
        if path.exists() and not refresh:
            return path.read_text()
        text = self._fetch(url, params).text
        path.write_text(text)
        log.info("fetched text %s %s", url, params or "")
        return text
