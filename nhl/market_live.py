"""Live prediction-market season POINTS totals for the upcoming NHL season (downstream comparison ONLY).

Same strict rule as the NBA project: **market prices are never a feature and never touch the
model.** This exists purely to place the market beside our projection in the NHL "Records" page,
so a reader can see *where* and *by how much* an analytically-driven model disagrees with the crowd
-- the whole deliverable.

Source. **Kalshi `KXNHLSEASONPTS`** ("NHL Season Point Totals") -- per-team season standings
**points** quoted as a *threshold ladder* ("85+ points", "90+ points", ...), which is a whole
implied distribution, not a single line, and in our model's exact target unit (no wins->points
conversion). All 32 teams were open on 2026-09-29, opening night. (The module used to read
`KXNHLWINS`, a wins series; it exists but has never listed an event, so it was dropped.)

Reconstruction (the NBA `market_live` algorithm, plus one NHL-specific guard). Each threshold market
prices P(points >= k) -- a non-increasing survival function S(k). From the ladder we recover the
**median** (S crosses 0.5), the **mean** (area under S), and **p10/p90** (S crosses 0.90/0.10).
MID of bid/ask is used, not the stale `last` on a thin book.

- **The median is the headline, not the mean.** Unlike the NBA's per-team ladders, Kalshi prices
  every NHL team on the same fixed 70..115 grid, so a strong team's upper tail (COL: 36% on 115+)
  and a weak team's lower tail (VAN: ~50% below 70) fall off the ladder. The mean and p10/p90
  anchor those tails one rung past the grid and get squashed toward the middle; the median sits
  inside the grid for every team, and it is the same kind of number as a sportsbook O/U line.
- **Rungs with a bid/ask spread wider than ``MAX_SPREAD`` are dropped as unpriced.** The book is
  thin: typical spreads are ~0.10, but a few rungs are quoted e.g. 0.00/0.76 -- a mid there is
  noise, not a price. A team whose median can't be read from the remaining rungs gets ``None``
  (the Records page then falls back to the Vegas line for it).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.request

from .ingest import DATA_DIR, PROC

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MARKET_RAW = DATA_DIR / "raw" / "market"
SERIES = "KXNHLSEASONPTS"
RUNG_STEP = 5      # threshold spacing; checked 2026-09-29 (70, 75, ..., 115 for every team)
MAX_SPREAD = 0.30  # ~3x the typical ~0.10 spread; 2026-09-29 had a clean gap between 0.25 and 0.36

# Kalshi's ticker suffix -> NHL tricode, where they differ (checked 2026-09-29).
TICKER_ALIASES = {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL"}


def _get(url: str, *, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _cache_path(season_yy: int):
    return MARKET_RAW / f"kalshi_nhlpts_{season_yy}.json"


def fetch_points_events(season_yy: int = 27, *, refresh: bool = False) -> list[dict]:
    """Open KXNHLSEASONPTS team events (with nested threshold markets) for the season-end year.

    Cached to disk keyed by the two-digit year; pass ``refresh=True`` to re-pull live quotes.
    Returns ``[]`` when the market is not posted.
    """
    MARKET_RAW.mkdir(parents=True, exist_ok=True)
    path = _cache_path(season_yy)
    if path.exists() and not refresh:
        return json.loads(path.read_text())["events"]

    url = (f"{KALSHI_BASE}/events?series_ticker={SERIES}&status=open"
           "&with_nested_markets=true&limit=200")
    payload = _get(url)
    events = [e for e in payload.get("events", [])
              if e["event_ticker"].startswith(f"{SERIES}-{season_yy}")]
    path.write_text(json.dumps({"fetched": time.time(), "events": events}))
    log.info("kalshi: %d NHL season-points events cached", len(events))
    return events


def fetched_date(season_yy: int = 27) -> str | None:
    """ISO date the cached quotes were pulled, or None if nothing is cached."""
    path = _cache_path(season_yy)
    if not path.exists():
        return None
    return dt.date.fromtimestamp(json.loads(path.read_text())["fetched"]).isoformat()


def _mid(market: dict) -> float | None:
    bid = float(market.get("yes_bid_dollars") or 0)
    ask = float(market.get("yes_ask_dollars") or 0)
    if ask - bid > MAX_SPREAD:
        return None                                   # quoted too wide to be a price
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    last = float(market.get("last_price_dollars") or 0)
    return last if last > 0 else None


def _ladder(event: dict) -> list[tuple[float, float]]:
    """(threshold, survival) points, cleaned to a valid non-increasing survival function."""
    pts: list[tuple[float, float]] = []
    for m in event.get("markets", []):
        k, s = m.get("floor_strike"), _mid(m)
        if k is None or s is None:
            continue
        pts.append((float(k), min(1.0, max(0.0, s))))
    pts.sort()
    for i in range(1, len(pts)):
        if pts[i][1] > pts[i - 1][1]:                 # survival can only fall as the bar rises
            pts[i] = (pts[i][0], pts[i - 1][1])
    return pts


def _cross(pts: list[tuple[float, float]], level: float) -> float | None:
    for i in range(len(pts) - 1):
        (k0, s0), (k1, s1) = pts[i], pts[i + 1]
        if s0 >= level >= s1 and s0 != s1:
            return k0 + (s0 - level) / (s0 - s1) * (k1 - k0)
    return None


def implied_distribution(event: dict) -> dict | None:
    """Median / mean / p10 / p90 (in POINTS) from one team's Kalshi threshold ladder.

    The median is read from the priced rungs only (``None`` if they don't bracket 0.5); mean and
    p10/p90 use the ladder extended one rung past each end, so they are approximate at the tails.
    """
    pts = _ladder(event)
    if len(pts) < 3:
        return None
    lo, hi = (pts[0][0] - RUNG_STEP, 1.0), (pts[-1][0] + RUNG_STEP, 0.0)
    curve = [lo] + pts + [hi]
    mean = sum((s0 + s1) / 2 * (k1 - k0) for (k0, s0), (k1, s1) in zip(curve, curve[1:])) + lo[0]
    median = _cross(pts, 0.5)
    return {
        "median": round(median, 1) if median is not None else None,
        "mean": round(mean, 1),
        "p10": _cross(curve, 0.90), "p90": _cross(curve, 0.10),
        "n_rungs": len(pts), "ladder": [[k, round(s, 3)] for k, s in pts],
    }


def _team_from_ticker(event_ticker: str, season_yy: int) -> str:
    """NHL tricode from a Kalshi event ticker (e.g. 'KXNHLSEASONPTS-27LA' -> 'LAK')."""
    code = event_ticker.replace(f"{SERIES}-{season_yy}", "").strip("-").upper()
    return TICKER_ALIASES.get(code, code)


def market_points_table(season_yy: int = 27, *, refresh: bool = False) -> dict[str, dict]:
    """Implied POINTS distribution per NHL tricode ``{tricode: {median, mean, p10, p90, ...}}``.

    Empty ``{}`` when the market is not posted. Raises if a ticker maps to an unknown tricode, so
    a Kalshi code change fails loud instead of silently dropping a team. Downstream-only: this
    output is shown beside the projection, never fed into it.
    """
    import pandas as pd

    events = fetch_points_events(season_yy, refresh=refresh)
    known = set(pd.read_parquet(PROC / "team_reference.parquet")["tricode"])
    out: dict[str, dict] = {}
    for ev in events:
        tri = _team_from_ticker(ev["event_ticker"], season_yy)
        if tri not in known:
            raise ValueError(f"kalshi ticker {ev['event_ticker']!r} -> unknown tricode {tri!r}; "
                             "add it to TICKER_ALIASES")
        dist = implied_distribution(ev)
        if dist is not None:
            out[tri] = dist
    return out
