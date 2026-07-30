"""Disk-backed cache and throttled fetch wrapper for nba_api.

stats.nba.com is rate-limited and intermittently drops requests. A full historical
pull is several hundred calls, so every fetch goes through here: cached to parquet
on disk, keyed by endpoint + params, with throttling and exponential backoff.

Re-running a fetch script is therefore cheap and idempotent, which is what makes
the walk-forward backtest practical to iterate on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# stats.nba.com tolerates roughly this cadence without shedding requests.
MIN_INTERVAL_S = 0.7
MAX_ATTEMPTS = 5

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_DIR = DATA_DIR / "raw"

_last_call_at = 0.0


def _throttle() -> None:
    """Space out consecutive live calls, with jitter to avoid a lockstep pattern."""
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    wait = MIN_INTERVAL_S - elapsed
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.25))
    _last_call_at = time.monotonic()


def _key(endpoint: str, params: dict) -> str:
    """Stable cache key. Params are sorted so key order can't split the cache."""
    blob = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.sha1(blob.encode()).hexdigest()[:12]
    return f"{endpoint}__{digest}"


def cached_fetch(
    endpoint: str,
    fetch_fn,
    params: dict,
    *,
    frame: int = 0,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return a DataFrame for this endpoint+params, from disk if we have it.

    ``fetch_fn`` is called as ``fetch_fn(**params)`` and must return an nba_api
    endpoint object exposing ``get_data_frames()``.

    An empty result is cached too. Empty is a real, meaningful answer here: it is
    how stats.nba.com reports "this metric did not exist in that season", and we
    do not want to re-request it on every run.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{_key(endpoint, params)}.parquet"

    if path.exists() and not refresh:
        return pd.read_parquet(path)

    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _throttle()
            df = fetch_fn(**params).get_data_frames()[frame]
            # Object-dtype columns from the API can be mixed; normalize so parquet
            # round-trips cleanly and dtypes are stable across cache hits/misses.
            for col in df.columns[df.dtypes.eq("object")]:
                df[col] = df[col].astype("string")
            df.to_parquet(path, index=False)
            log.info("fetched %s %s -> %d rows", endpoint, params, len(df))
            return df
        except Exception as err:  # noqa: BLE001 - API raises a variety of transport errors
            last_err = err
            backoff = min(2**attempt, 30) + random.uniform(0, 1)
            log.warning(
                "%s attempt %d/%d failed (%s); retrying in %.1fs",
                endpoint, attempt, MAX_ATTEMPTS, type(err).__name__, backoff,
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"{endpoint} failed after {MAX_ATTEMPTS} attempts with {params}"
    ) from last_err


def season_str(start_year: int) -> str:
    """2015 -> '2015-16'. The API's season format."""
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def seasons(first: int, last: int) -> list[str]:
    """Inclusive range of season strings by start year."""
    return [season_str(y) for y in range(first, last + 1)]
