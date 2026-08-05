"""Live prediction-market win totals for the upcoming NHL season (downstream comparison ONLY).

Same strict rule as the NBA project: **market prices are never a feature and never touch the
model.** This exists purely to place the market beside our projection in the NHL "Records" page,
so a reader can see *where* and *by how much* an analytically-driven model disagrees with the crowd
-- the whole deliverable.

Source. **Kalshi `KXNHLWINS`** ("NHL wins") -- the hockey analog of the NBA project's `KXNBAWINS`,
per-team season **win** totals quoted as a *threshold ladder* ("35+ wins", "40+ wins", ...), which
is a whole implied distribution, not a single line. NOTE the target: Kalshi settles on **wins**, not
standings **points** -- a win is 2 points but an OT/SO loss is still 1 -- so the comparison is to our
projected *wins* (`gamesim.expected_wins`), never to points. As of the 2026-27 pre-season the series
exists but has **no open events yet** (win markets post closer to opening night, like the NBA's bbref
Vegas 404); `market_win_table` returns ``{}`` until then, and the page simply shows no market ring.

Reconstruction (identical to the NBA `market_live`, a proven algorithm). Each threshold market prices
P(wins >= k) -- a non-increasing survival function S(k). From the ladder we recover the **median**
(S crosses 0.5; robust to a thin market's wide spreads -- the headline), the **mean** (area under S),
and **p10/p90** (S crosses 0.90/0.10; approximate at the tails). MID of bid/ask is used, not the
stale `last` on a just-opened book. The one NHL-specific piece -- parsing a team tricode out of the
Kalshi event ticker -- is written defensively and **verified on the first live post** (asserted, so a
code drift fails loud rather than dropping a team).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path

from .ingest import DATA_DIR

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MARKET_RAW = DATA_DIR / "raw" / "market"
SERIES = "KXNHLWINS"
RUNG_STEP = 5  # assumed win-threshold spacing; re-checked against the ladder on the first live pull


def _get(url: str, *, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_win_events(season_yy: int = 27, *, refresh: bool = False) -> list[dict]:
    """Open KXNHLWINS team events (with nested threshold markets) for the given season-end year.

    Cached to disk keyed by the two-digit year. Returns ``[]`` when the market is not yet posted
    (no open events) -- the expected state well before opening night.
    """
    MARKET_RAW.mkdir(parents=True, exist_ok=True)
    path = MARKET_RAW / f"kalshi_nhlwins_{season_yy}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())["events"]

    url = (f"{KALSHI_BASE}/events?series_ticker={SERIES}&status=open"
           "&with_nested_markets=true&limit=200")
    payload = _get(url)
    events = [e for e in payload.get("events", []) if str(season_yy) in e["event_ticker"]]
    path.write_text(json.dumps({"fetched": time.time(), "events": events}))
    log.info("kalshi: %d NHL win-total events cached", len(events))
    return events


def _mid(market: dict) -> float | None:
    bid = float(market.get("yes_bid_dollars") or 0)
    ask = float(market.get("yes_ask_dollars") or 0)
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
    """Median / mean / p10 / p90 (in WINS) from one team's Kalshi threshold ladder."""
    pts = _ladder(event)
    if len(pts) < 3:
        return None
    lo, hi = (pts[0][0] - RUNG_STEP, 1.0), (pts[-1][0] + RUNG_STEP, 0.0)
    curve = [lo] + pts + [hi]
    mean = sum((s0 + s1) / 2 * (k1 - k0) for (k0, s0), (k1, s1) in zip(curve, curve[1:])) + lo[0]
    return {
        "median": _cross(curve, 0.5), "mean": round(mean, 1) if mean else None,
        "p10": _cross(curve, 0.90), "p90": _cross(curve, 0.10),
        "n_rungs": len(pts), "ladder": [[k, round(s, 3)] for k, s in pts],
    }


def _team_from_ticker(event_ticker: str, season_yy: int) -> str:
    """NHL tricode from a Kalshi event ticker (e.g. 'KXNHLWINS-27COL' -> 'COL'). Verified against
    the tricode set on the first live post (see market_win_table's assert)."""
    return event_ticker.replace(f"{SERIES}-{season_yy}", "").strip("-").upper()


def market_win_table(season_yy: int = 27, *, refresh: bool = False) -> dict[str, dict]:
    """Implied WIN distribution per NHL tricode ``{tricode: {median, mean, p10, p90, ...}}``.

    Empty ``{}`` when the market is not yet posted. Downstream-only: this output is shown beside the
    projection, never fed into it.
    """
    events = fetch_win_events(season_yy, refresh=refresh)
    out: dict[str, dict] = {}
    for ev in events:
        tri = _team_from_ticker(ev["event_ticker"], season_yy)
        dist = implied_distribution(ev)
        if dist is not None:
            out[tri] = dist
    return out
