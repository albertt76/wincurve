"""Live prediction-market win totals for the upcoming season (downstream comparison only).

The project's rule is strict: **market prices are never a feature and never touch training.**
This module exists purely to place the market beside our own projection in the UI, so a
reader can see *where* and *by how much* an analytically-driven model disagrees with the
crowd -- the whole deliverable of wincurve.

Source, and why only one
-------------------------
For the 2026-27 season three markets were checked (July 2026):

* **Kalshi** `KXNBAWINS` -- per-team season win totals for all 30 teams, quoted as a
  *threshold ladder* ("20+ wins", "25+ wins", ...). This is richer than a sportsbook's
  single over/under line: the ladder is a whole implied distribution, so we can recover a
  market median AND an approximate market 80% range and compare distribution-to-distribution.
  Reads are unauthenticated. **This is the source.**
* **Basketball-Reference** preseason Vegas over/under -- still a 404 for NBA_2027 (not
  posted this early). Historical Vegas lines remain the backtest baseline in `odds.py`.
* **Polymarket** -- has NBA champion / conference / make-the-playoffs / ROY markets, but
  **no per-team season win-total market**, so nothing win-total-comparable to show.

Reconstructing a win total from a ladder
-----------------------------------------
Each threshold market prices P(wins >= k) -- a *survival function* S(k), non-increasing in
k. From the ladder we recover:

* **median** -- the k where S(k) crosses 0.5 (robust to the wide bid/ask spreads on a thin,
  just-opened market; this is the headline comparison number).
* **mean** -- area under the survival curve (E[W] = integral of S), trapezoid-integrated
  between ladder points with S=1 anchored just below the lowest rung and S=0 just above the
  highest. Apples-to-apples with our simulated mean.
* **p10 / p90** -- where S crosses 0.90 / 0.10, giving a market 80% range to set against
  ours. Approximate at the tails (thin books, 5-win rungs) and flagged as such.

MID prices are used -- (bid + ask) / 2 -- because a brand-new market's `last` trade is stale
and its spreads are wide. Wide spreads mean the implied probabilities are noisy; the median
survives that far better than the tails, which is why the median is the headline.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from sklearn.isotonic import IsotonicRegression

from .cache import DATA_DIR

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MARKET_RAW = DATA_DIR / "raw" / "market"
RUNG_STEP = 5  # Kalshi lists win thresholds every 5 wins

# Kalshi's per-team event code is the standard NBA abbreviation, which is exactly the
# abbreviation nba_api uses -- so the join key is the abbreviation directly. Asserted on
# merge so a silent franchise-code drift (PHX/PHO, BKN/BRK) fails loudly rather than
# quietly dropping a team from the comparison.


def _get(url: str, *, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def fetch_win_events(season_yy: int = 27, *, refresh: bool = False) -> list[dict]:
    """All 30 team events (with nested threshold markets) for the KXNBAWINS series.

    Cached to disk keyed by the two-digit season-end year. Pass ``refresh=True`` to re-pull
    a fresher snapshot mid-season; the cache file always holds the most recent pull.
    """
    MARKET_RAW.mkdir(parents=True, exist_ok=True)
    path = MARKET_RAW / f"kalshi_nbawins_{season_yy}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())["events"]

    url = (
        f"{KALSHI_BASE}/events?series_ticker=KXNBAWINS&status=open"
        "&with_nested_markets=true&limit=200"
    )
    try:
        payload = _get(url)
    except urllib.error.HTTPError as err:  # pragma: no cover - network
        log.error("Kalshi fetch failed: HTTP %s", err.code)
        raise
    events = [e for e in payload.get("events", []) if str(season_yy) in e["event_ticker"]]
    path.write_text(json.dumps({"fetched": time.time(), "events": events}))
    log.info("kalshi: %d team win-total events cached", len(events))
    return events


def _mid(market: dict) -> float | None:
    """Fair-value proxy for one threshold market: mid of bid/ask, else the last trade."""
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
    """(threshold, survival) points, cleaned to a valid non-increasing survival function.

    A survival function P(wins >= k) can only fall as k rises, but individual rungs are
    noisy -- a resting bid nobody has updated, or a market with no two-sided quote at all.
    An earlier version of this function fixed inversions by CLAMPING every later point
    down to match an earlier noisy one (forward-only). That lets one bad rung corrupt
    every rung above it. Found via a real case: IND's 2026-07-30 snapshot had a wide
    ($0.59 bid / $0.99 ask, mid $0.79) "10+ wins" quote sitting below five tighter,
    more-liquid quotes above it (all ~$0.92-$0.955) -- the forward clamp dragged all five
    down to $0.79, which corrupted the reconstructed mean by ~5 wins (38.9 vs a same-day
    median of 43.9, and an implied 10th-percentile of just 7.4 wins for a playoff-caliber
    roster). Fixed with an ISOTONIC regression across all rungs at once (fits the closest
    non-increasing curve in a weighted-least-squares sense), weighted by each rung's own
    liquidity (inverse bid/ask spread): a tight two-sided quote pulls the fit toward
    itself, a wide or one-sided quote is trusted less and gets pooled with its neighbors
    instead of dragging them down.
    """
    pts: list[tuple[float, float, float]] = []
    for m in event.get("markets", []):
        k = m.get("floor_strike")
        s = _mid(m)
        if k is None or s is None:
            continue
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
        # A one-sided quote (only a bid, only an ask, or a stale last-trade fallback) has
        # no observed spread to judge it by -- treat it as maximally wide (least trusted).
        spread = (ask - bid) if (bid > 0 and ask > 0) else 1.0
        weight = 1.0 / max(spread, 0.02)
        pts.append((float(k), min(1.0, max(0.0, s)), weight))
    pts.sort()
    if len(pts) < 2:
        return [(k, s) for k, s, _ in pts]
    ks = [p[0] for p in pts]
    ss = [p[1] for p in pts]
    ws = [p[2] for p in pts]
    iso = IsotonicRegression(increasing=False, y_min=0.0, y_max=1.0)
    fitted = iso.fit_transform(ks, ss, sample_weight=ws)
    return list(zip(ks, (float(v) for v in fitted)))


def _cross(pts: list[tuple[float, float]], level: float) -> float | None:
    """Linear-interpolate the threshold at which survival == level."""
    for i in range(len(pts) - 1):
        (k0, s0), (k1, s1) = pts[i], pts[i + 1]
        if s0 >= level >= s1 and s0 != s1:
            return k0 + (s0 - level) / (s0 - s1) * (k1 - k0)
    return None


def implied_distribution(event: dict) -> dict | None:
    """Recover median / mean / p10 / p90 (in wins) from one team's threshold ladder."""
    pts = _ladder(event)
    if len(pts) < 3:
        return None

    # Anchor the survival curve: certainly wins at least (lowest_rung - step), certainly does
    # not reach (highest_rung + step). Lets both the tail quantiles and the integral close.
    lo = (pts[0][0] - RUNG_STEP, 1.0)
    hi = (pts[-1][0] + RUNG_STEP, 0.0)
    curve = [lo] + pts + [hi]

    # Mean = area under the survival curve (E[W] = integral_0^inf P(W >= w) dw), trapezoid.
    mean = 0.0
    for (k0, s0), (k1, s1) in zip(curve, curve[1:]):
        mean += (s0 + s1) / 2 * (k1 - k0)
    mean += lo[0]  # the certain wins below the first anchor

    median = _cross(curve, 0.5)
    p10 = _cross(curve, 0.90)  # 10th percentile of wins = 90% survival
    p90 = _cross(curve, 0.10)
    return {
        "median": median,
        "mean": round(mean, 1) if mean else None,
        "p10": p10,
        "p90": p90,
        "n_rungs": len(pts),
        "ladder": [[k, round(s, 3)] for k, s in pts],
    }


def market_win_table(season_yy: int = 27, *, refresh: bool = False) -> dict[str, dict]:
    """Implied win distribution per team abbreviation for the given season.

    Returns ``{abbr: {median, mean, p10, p90, ...}}``. Team codes are NBA-standard
    abbreviations, joinable directly onto our snapshot rows.
    """
    events = fetch_win_events(season_yy, refresh=refresh)
    out: dict[str, dict] = {}
    for ev in events:
        abbr = ev["event_ticker"].replace(f"KXNBAWINS-{season_yy}", "")
        dist = implied_distribution(ev)
        if dist is None:
            log.warning("kalshi: %s has too thin a ladder to price", abbr)
            continue
        out[abbr] = dist
    return out
