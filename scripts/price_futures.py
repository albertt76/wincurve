"""Price Kalshi's NBA season-long markets from the model, with the model's edge shrunk to what has
actually shown up out of sample, and size a bankroll across the ones that survive.

Why this exists: the win-total readout compares our mean to the market's mean. A bet needs more:
a probability for the specific contract (win ladder rungs, make-the-playoffs, division, #1 seed,
best/worst record, conference, title), the price actually available (ask, depth, fee), and an
honest view of how much of our gap to the market is real.

How it works
  1. HOW MUCH OF OUR GAP IS REAL.  Over the completed seasons in snapshots.json, regress
     (actual - market) on (ours - market) through the origin. The slope b (0.32 +/- 0.11 as of
     2026-10) is the share of a gap that showed up in results. The calibrated mean for a team is
     anchor + b * (ours - anchor), where the anchor is the market's own mean. b is measured against
     the preseason Vegas line, so the anchor here averages Kalshi's ladder mean with the sportsbook
     line (data/overrides/sportsbook_win_totals.json) when one is on file.
  2. ONE JOINT SEASON SIMULATION (all 30 teams, real schedule, per-team rating uncertainty, the
     model's home-court and margin parameters), re-run with team ratings shifted so each team's
     mean wins hits its calibrated target. Standings, the play-in tournament, and the playoff
     bracket are simulated inside every season, so every market is priced from the same worlds
     and correlated outcomes stay correlated. Four scenarios share random numbers: b = 0 (the
     market is right), b - 1 SE, b, b + 1 SE.
  3. PRICE EVERY LIVE CONTRACT on both sides (buy YES at the ask, buy NO at 1 - bid), net of
     Kalshi's taker fee (0.07 * p * (1 - p) per contract), and size by quarter-Kelly on the b - 1 SE
     probability with per-position, per-team and total caps.

Honest limits, printed with the output: the playoff stage (champion / conference) is not
backtested, so those probabilities are additionally blended halfway toward the market; the
sportsbook file is hand-copied and can be days stale; and a Kalshi quote is a snapshot -- check
the live order book (and the size shown) before acting.

    python scripts/price_futures.py                  # fetch Kalshi, price, size a $1000 bankroll
    python scripts/price_futures.py --bank 500 --sims 20000 --no-fetch
Output: a printed report and data/processed/futures_pricing.json (gitignored).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.special import erf, ndtri  # noqa: E402

from nbaproj.market_live import KALSHI_BASE  # noqa: E402
from nbaproj.simulate import estimate_game_params, extract_schedule  # noqa: E402

PROC = ROOT / "data" / "processed"
RAW = ROOT / "data" / "raw" / "market"
SPORTSBOOK = ROOT / "data" / "overrides" / "sportsbook_win_totals.json"
TARGET, LAST_HISTORY = 2026, 2025
SQRT2 = math.sqrt(2.0)
WINS_PER_RATING = 2.38          # one team's mean wins per rating point (project_current's grid)
MARKET_SD = 8.6                 # realised SD of actual wins around the market (market MAE 6.9)
FEE_RATE = 0.07                 # Kalshi taker fee: 0.07 * p * (1 - p) per contract
PLAYOFF_BLEND = 0.5             # playoff-stage probabilities: share given to the market price

EAST = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NYK", "ORL", "PHI", "TOR", "WAS"}
DIVISIONS = {
    "ATLANTIC": ["BOS", "BKN", "NYK", "PHI", "TOR"], "CENTRAL": ["CHI", "CLE", "DET", "IND", "MIL"],
    "SOUTHEAST": ["ATL", "CHA", "MIA", "ORL", "WAS"], "NORTHWEST": ["DEN", "MIN", "OKC", "POR", "UTA"],
    "PACIFIC": ["GSW", "LAC", "LAL", "PHX", "SAC"], "SOUTHWEST": ["DAL", "HOU", "MEM", "NOP", "SAS"],
}
KALSHI_SERIES = ["KXNBAWINS", "KXNBA", "KXNBAEAST", "KXNBAWEST", "KXNBAPLAYOFF", "KXNBAPLAYIN",
                 "KXNBAEAST1SEED", "KXNBAWEST1SEED", "KXNBAMOSTWINS", "KXNBARECORD",
                 "KXNBAATLANTIC", "KXNBACENTRAL", "KXNBASOUTHEAST", "KXNBANORTHWEST",
                 "KXNBAPACIFIC", "KXNBASOUTHWEST"]
PLAYOFF_KINDS = {"champ", "conf"}


# ---------------------------------------------------------------------------------- calibration
def _gap_data() -> tuple[np.ndarray, np.ndarray]:
    """(ours - market, actual - market) for every completed team-season with a market line."""
    snaps = json.loads((PROC / "snapshots.json").read_text())["snapshots"]
    xs, ys = [], []
    for snap in snaps.values():
        if snap.get("is_current"):
            continue
        for t in snap["teams"]:
            if t.get("mkt_wins") is None or t.get("actual") is None:
                continue
            xs.append(t["wins"] - t["mkt_wins"])
            ys.append(t["actual"] - t["mkt_wins"])
    return np.array(xs), np.array(ys)


def gap_slope() -> tuple[float, float, int]:
    """Share of (ours - market) that showed up in results: slope through the origin of
    (actual - market) on (ours - market) over the completed seasons. Returns (b, SE, n)."""
    x, y = _gap_data()
    b = float((x * y).sum() / (x * x).sum())
    res = y - b * x
    se = float(np.sqrt((res ** 2).sum() / (len(x) - 1) / (x * x).sum()))
    return b, se, len(x)


def residual_sd(lam: float) -> float:
    """SD (standard deviation: typical distance from the average) of actual wins around a mean
    that takes `lam` of our gap to the market. ~8.7 at lam=0, ~8.5 near b, ~9.3 at lam=1."""
    x, y = _gap_data()
    return float((y - lam * x).std())


def american_to_prob(o: float) -> float:
    return -o / (-o + 100.0) if o < 0 else 100.0 / (o + 100.0)


def anchors(abbrs: list[str], model_mean: dict[str, float]) -> tuple[dict, dict]:
    """Market-mean anchor per team (average of Kalshi's ladder mean and the sportsbook line's
    implied mean), plus the pieces for the report."""
    kal = json.loads((PROC / "market_2026_27.json").read_text())["teams"]
    book = json.loads(SPORTSBOOK.read_text())["lines"] if SPORTSBOOK.exists() else {}
    out, parts = {}, {}
    for a in abbrs:
        k = float(kal[a]["wins"])
        sb = None
        if a in book:
            po, pu = american_to_prob(book[a]["over"]), american_to_prob(book[a]["under"])
            sb = book[a]["line"] + MARKET_SD * float(ndtri(po / (po + pu)))
        out[a] = k if sb is None else 0.5 * (k + sb)
        parts[a] = {"kalshi": k, "book": sb, "line": book.get(a, {}).get("line")}
    # league wins are zero-sum
    shift = (sum(out.values()) - 30 * 41.0) / 30.0
    return {a: v - shift for a, v in out.items()}, parts


# ------------------------------------------------------------------------------- simulation core
class Season:
    def __init__(self, teams: list[dict]):
        self.abbr = [t["abbr"] for t in teams]
        self.ids = [int(t["id"]) for t in teams]
        self.rating = np.array([t["rating"] for t in teams], float)
        self.sigma = np.array([t["sigma"] for t in teams], float)
        self.model_mean = np.array([t["wins"] for t in teams], float)
        gl = pd.read_parquet(PROC / "game_log.parquet")
        self.hca, self.msd = estimate_game_params(gl, before_season=TARGET)
        sched = extract_schedule(gl, LAST_HISTORY)
        idx = {t: i for i, t in enumerate(self.ids)}
        self.home = np.array([idx[x] for x in sched["home_id"]])
        self.away = np.array([idx[x] for x in sched["away_id"]])
        G, T = len(self.home), len(teams)
        self.H = np.zeros((G, T), np.float32)
        self.A = np.zeros((G, T), np.float32)
        self.H[np.arange(G), self.home] = 1
        self.A[np.arange(G), self.away] = 1
        self.hca_g = np.where(sched["neutral"].to_numpy(bool), 0.0, self.hca)
        self.T = T
        self.sigma_scale = 1.0
        self.east = np.array([i for i, a in enumerate(self.abbr) if a in EAST])
        self.west = np.array([i for i, a in enumerate(self.abbr) if a not in EAST])

    def simulate(self, base: np.ndarray, n: int, seed: int, chunk: int = 4000):
        rng = np.random.default_rng(seed)
        wins = np.empty((n, self.T), np.int16)
        tr = np.empty((n, self.T), np.float32)
        for c0 in range(0, n, chunk):
            m = min(chunk, n - c0)
            t = base[None, :] + rng.standard_normal((m, self.T)) * (self.sigma * self.sigma_scale)[None, :]
            em = t[:, self.home] - t[:, self.away] + self.hca_g[None, :]
            p = 0.5 * (1.0 + erf(em / (self.msd * SQRT2)))
            hw = (rng.random(em.shape) < p).astype(np.float32)
            wins[c0:c0 + m] = np.rint(hw @ self.H + (1.0 - hw) @ self.A).astype(np.int16)
            tr[c0:c0 + m] = t
        return wins, tr

    def match_spread(self, target_sd: float, seed: int = 5, n: int = 6000) -> float:
        """Scale the rating uncertainty so the average simulated SD of a team's wins equals
        `target_sd`. The shipped sigma was fitted around the unshrunk model (SD ~9.3); around a
        mean pulled toward the market the backtested miss is narrower (~8.5), and an over-wide
        simulation manufactures edge in the tails of every threshold contract."""
        self.sigma_scale = 1.0
        w, _ = self.simulate(self.rating, n, seed)
        full = float(w.var(axis=0).mean())
        keep, self.sigma = self.sigma, self.sigma * 0.0
        w0, _ = self.simulate(self.rating, n, seed)
        self.sigma = keep
        game = float(w0.var(axis=0).mean())
        self.sigma_scale = math.sqrt(max(target_sd ** 2 - game, 1.0) / max(full - game, 1e-6))
        return self.sigma_scale

    def calibrate(self, target: np.ndarray, seed: int, n: int = 6000) -> np.ndarray:
        """Shift ratings until each team's simulated mean wins hits `target` (common random
        numbers, so the solve is smooth)."""
        base = self.rating.copy()
        for _ in range(3):
            w, _ = self.simulate(base, n, seed)
            err = target - w.mean(axis=0)
            if np.abs(err).max() < 0.12:
                break
            base = base + err / WINS_PER_RATING
        return base

    # ------------------------------------------------------------- standings and playoffs
    def outcomes(self, wins: np.ndarray, tr: np.ndarray, seed: int) -> dict[str, np.ndarray]:
        S, T = wins.shape
        rng = np.random.default_rng(seed)
        ar = np.arange(S)
        score = wins + rng.random((S, T)) * 0.5          # random tie-break
        pw = lambda h, a: 0.5 * (1.0 + erf((tr[ar, h] - tr[ar, a] + self.hca) / (self.msd * SQRT2)))  # noqa: E731

        def series(ta, sa, tb, sb):
            hi = np.where(sa < sb, ta, tb)
            lo = np.where(sa < sb, tb, ta)
            d = tr[ar, hi] - tr[ar, lo]
            p_home = 0.5 * (1.0 + erf((d + self.hca) / (self.msd * SQRT2)))
            p_away = 0.5 * (1.0 + erf((d - self.hca) / (self.msd * SQRT2)))
            w = np.zeros(S)
            for hosts_hi in (True, True, False, False, True, False, True):   # 2-2-1-1-1
                w += rng.random(S) < (p_home if hosts_hi else p_away)
            hi_won = w >= 4
            return np.where(hi_won, hi, lo), np.where(hi_won, np.minimum(sa, sb), np.maximum(sa, sb))

        flags = {k: np.zeros((S, T), bool) for k in
                 ("made", "playin", "seed1", "div", "best", "worst", "conf", "champ", "finals")}
        champs = {}
        for name, conf in (("east", self.east), ("west", self.west)):
            order = np.argsort(-score[:, conf], axis=1)
            seeded = conf[order]                                    # (S, 15) team index by seed
            s7, s8 = seeded[:, 6], seeded[:, 7]
            w78 = rng.random(S) < pw(s7, s8)
            seed7, loser = np.where(w78, s7, s8), np.where(w78, s8, s7)
            w910 = np.where(rng.random(S) < pw(seeded[:, 8], seeded[:, 9]), seeded[:, 8], seeded[:, 9])
            seed8 = np.where(rng.random(S) < pw(loser, w910), loser, w910)
            bracket = np.column_stack([seeded[:, :6], seed7, seed8])    # seeds 1..8
            flags["seed1"][ar, seeded[:, 0]] = True
            for j in range(8):
                flags["made"][ar, bracket[:, j]] = True
            for j in range(6, 10):
                flags["playin"][ar, seeded[:, j]] = True
            r1 = [series(bracket[:, a], a + 1, bracket[:, b], b + 1) for a, b in ((0, 7), (3, 4), (2, 5), (1, 6))]
            sf1 = series(r1[0][0], r1[0][1], r1[1][0], r1[1][1])
            sf2 = series(r1[3][0], r1[3][1], r1[2][0], r1[2][1])
            champs[name] = series(sf1[0], sf1[1], sf2[0], sf2[1])[0]
            flags["conf"][ar, champs[name]] = True
        # finals: higher regular-season score hosts first
        e, w = champs["east"], champs["west"]
        e_hi = score[ar, e] >= score[ar, w]
        hi, lo = np.where(e_hi, e, w), np.where(e_hi, w, e)
        d = tr[ar, hi] - tr[ar, lo]
        p_home = 0.5 * (1.0 + erf((d + self.hca) / (self.msd * SQRT2)))
        p_away = 0.5 * (1.0 + erf((d - self.hca) / (self.msd * SQRT2)))
        wn = np.zeros(S)
        for hosts_hi in (True, True, False, False, True, False, True):
            wn += rng.random(S) < (p_home if hosts_hi else p_away)
        flags["champ"][ar, np.where(wn >= 4, hi, lo)] = True
        flags["finals"][ar, e] = True
        flags["finals"][ar, w] = True
        flags["best"][ar, np.argmax(score, axis=1)] = True
        flags["worst"][ar, np.argmin(score, axis=1)] = True
        return flags


def add_division_flags(flags: dict, season: Season, wins: np.ndarray, seed: int) -> None:
    S = wins.shape[0]
    ar = np.arange(S)
    score = wins + np.random.default_rng(seed + 99).random(wins.shape) * 0.5
    for div, members in DIVISIONS.items():
        cols = np.array([season.abbr.index(a) for a in members])
        arr = np.zeros(wins.shape, bool)
        arr[ar, cols[np.argmax(score[:, cols], axis=1)]] = True
        flags["div_" + div] = arr
    flags["max_wins"] = wins.max(axis=1)


# ------------------------------------------------------------------------------- Kalshi markets
def _get(url: str, tries: int = 6) -> dict:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            if err.code == 429 and i < tries - 1:
                time.sleep(2.0 * (i + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def fetch_markets(refresh: bool) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / "kalshi_futures_27.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    out = {}
    for s in KALSHI_SERIES:
        d = _get(f"{KALSHI_BASE}/events?series_ticker={s}&status=open&with_nested_markets=true&limit=200")
        out[s] = d.get("events", [])
        time.sleep(1.0)       # Kalshi rate-limits bursts (HTTP 429)
    payload = {"fetched": time.time(), "series": out}
    path.write_text(json.dumps(payload))
    return payload


def parse_markets(payload: dict, abbrs: list[str]) -> list[dict]:
    """Flatten Kalshi events into one row per contract with the fields we price on."""
    ab = set(abbrs)
    rows = []
    for series, events in payload["series"].items():
        for e in events:
            et = e["event_ticker"]
            for m in e.get("markets", []):
                if m.get("status") not in (None, "active"):
                    continue
                tail = m["ticker"].split("-")[-1]
                kind = team = None
                k = None
                if series == "KXNBAWINS":
                    team, kind, k = et.replace("KXNBAWINS-27", ""), "wins", int(m["floor_strike"])
                    label = f"{team} {k}+ wins"
                elif series == "KXNBA":
                    team, kind, label = tail, "champ", f"{tail} win the title"
                elif series in ("KXNBAEAST", "KXNBAWEST"):
                    team, kind, label = tail, "conf", f"{tail} win the {'East' if series.endswith('EAST') else 'West'}"
                elif series == "KXNBAPLAYOFF":
                    team, kind, label = tail, "made", f"{tail} make the playoffs"
                elif series == "KXNBAPLAYIN":
                    team, kind, label = tail, "playin", f"{tail} reach the play-in"
                elif series in ("KXNBAEAST1SEED", "KXNBAWEST1SEED"):
                    team, kind, label = tail, "seed1", f"{tail} the #1 seed ({'East' if 'EAST' in series else 'West'})"
                elif series == "KXNBARECORD":
                    kind = "best" if et.endswith("BEST") else "worst"
                    team, label = tail, f"{tail} {'best' if kind == 'best' else 'WORST'} record"
                elif series == "KXNBAMOSTWINS":
                    kind, k, label = "maxwins", int(m["floor_strike"]), f"any team wins {int(m['floor_strike'])}+"
                elif series[5:] in DIVISIONS:
                    team, kind, label = tail, "div_" + series[5:], f"{tail} win the {series[5:].title()}"
                if kind is None or (team is not None and team not in ab):
                    continue
                f = lambda key: float(m.get(key) or 0)  # noqa: E731
                rows.append({"series": series, "ticker": m["ticker"], "kind": kind, "team": team, "k": k,
                             "label": label, "bid": f("yes_bid_dollars"), "ask": f("yes_ask_dollars"),
                             "ask_size": f("yes_ask_size_fp"), "bid_size": f("yes_bid_size_fp"),
                             "volume": f("volume_fp"), "open_interest": f("open_interest_fp")})
    return rows


def yes_prob(row: dict, wins: np.ndarray, flags: dict, abbr_idx: dict) -> float:
    kind = row["kind"]
    if kind == "wins":
        return float((wins[:, abbr_idx[row["team"]]] >= row["k"]).mean())
    if kind == "maxwins":
        return float((flags["max_wins"] >= row["k"]).mean())
    return float(flags[kind][:, abbr_idx[row["team"]]].mean())


# ------------------------------------------------------------------------------------ pricing
def price(rows, results, abbr_idx):
    """One record per (contract, side) with our probability under every scenario."""
    recs = []
    for r in rows:
        py = {sc: yes_prob(r, wins, flags, abbr_idx) for sc, (wins, flags) in results.items()}
        mid = (r["bid"] + r["ask"]) / 2 if (r["bid"] > 0 and r["ask"] > 0) else None
        if r["kind"] in PLAYOFF_KINDS and mid is not None:     # the playoff stage is unvalidated
            py = {sc: PLAYOFF_BLEND * mid + (1 - PLAYOFF_BLEND) * p for sc, p in py.items()}
        for side in ("YES", "NO"):
            if side == "YES":
                cost, size, p = r["ask"], r["ask_size"], py
            else:
                cost, size = (1.0 - r["bid"]) if r["bid"] > 0 else 0.0, r["bid_size"]
                p = {sc: 1.0 - v for sc, v in py.items()}
            if not (0.02 <= cost <= 0.98):
                continue
            fee = FEE_RATE * cost * (1 - cost)
            s = cost + fee
            recs.append({**r, "side": side, "cost": cost, "fee": fee, "outlay": s, "depth_usd": size * cost,
                         "market_p": (mid if side == "YES" else 1 - mid) if mid is not None else None,
                         "p": p, "ev": {sc: p[sc] - s for sc in p}, "roi": {sc: (p[sc] - s) / s for sc in p},
                         "multiple": 1.0 / s - 1.0})
    return recs


def payoff_matrix(cands, results, abbr_idx, scenario):
    """(sims x candidates) profit per $1 spent, in the given scenario's simulated seasons."""
    wins, flags = results[scenario]
    cols = []
    for c in cands:
        if c["kind"] == "wins":
            hit = wins[:, abbr_idx[c["team"]]] >= c["k"]
        elif c["kind"] == "maxwins":
            hit = flags["max_wins"] >= c["k"]
        else:
            hit = flags[c["kind"]][:, abbr_idx[c["team"]]]
        hit = hit if c["side"] == "YES" else ~hit
        cols.append(np.where(hit, 1.0 / c["outlay"] - 1.0, -1.0))
    return np.column_stack(cols)


def optimise(cands, R, ub, per_team, total, frac):
    """Max expected log wealth over the simulated seasons (correlation handled by the joint sim),
    then scaled by `frac` (fractional Kelly: our probabilities are estimates). `ub` is the
    per-contract upper bound on the weight (a share of the bankroll)."""
    from scipy.optimize import minimize
    n = len(cands)
    teams = sorted({c["team"] for c in cands if c["team"]})
    A_team = np.array([[1.0 if c["team"] == t else 0.0 for c in cands] for t in teams])

    def f(x):
        w = 1.0 + R @ x
        if w.min() <= 1e-9:
            return 1e9, np.zeros(n)
        return -np.log(w).mean(), -(R / w[:, None]).mean(axis=0)

    cons = [{"type": "ineq", "fun": lambda x: total - x.sum(), "jac": lambda x: -np.ones(n)}]
    if len(teams):
        cons.append({"type": "ineq", "fun": lambda x: per_team - A_team @ x, "jac": lambda x: -A_team})
    ub = np.broadcast_to(np.asarray(ub, float), (n,))
    x0 = np.minimum(np.full(n, 0.002), ub)
    res = minimize(f, x0, jac=True, method="SLSQP", bounds=[(0.0, float(u)) for u in ub], constraints=cons,
                   options={"maxiter": 400, "ftol": 1e-10})
    return np.clip(res.x, 0.0, None) * frac


# ------------------------------------------------------------------------------------- report
LEGEND = """Legend: EV (expected value -- the average profit per contract if our probability is right, after
the fee); ROI (return on investment -- EV divided by what you pay); SE (standard error -- how
uncertain an estimate is); "payout" is profit per $1 staked if the contract wins; p(ours) is the
probability our simulation gives the contract, shown at b (the measured share of our gap that has
held up) and at b - 1 SE (a cautious case)."""


def pct(x):
    return "   n/a" if x is None else f"{100 * x:5.1f}%"


def stats(pnl):
    return {"mean": float(pnl.mean()), "median": float(np.median(pnl)), "p_loss": float((pnl < 0).mean()),
            "p5": float(np.percentile(pnl, 5)), "p95": float(np.percentile(pnl, 95)),
            "best": float(pnl.max()), "worst": float(pnl.min())}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bank", type=float, default=1000.0)
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--no-fetch", action="store_true", help="use the cached Kalshi pull")
    ap.add_argument("--frac", type=float, default=0.25, help="fraction of full Kelly (default quarter)")
    ap.add_argument("--per-pos", type=float, default=0.06, help="max share of bank in one contract")
    ap.add_argument("--per-team", type=float, default=0.12, help="max share of bank on one team")
    ap.add_argument("--total", type=float, default=0.60, help="max share of bank deployed")
    ap.add_argument("--max-spread", type=float, default=0.06, help="max bid-ask gap to consider")
    ap.add_argument("--min-depth", type=float, default=10.0, help="min dollars resting at the quote")
    ap.add_argument("--min-roi", type=float, default=0.10, help="min ROI at b - 1 SE")
    args = ap.parse_args()

    teams = json.loads((PROC / "projections_current.json").read_text())["teams"]
    season = Season(teams)
    abbrs = season.abbr
    idx = {a: i for i, a in enumerate(abbrs)}
    ours = season.model_mean
    anch, parts = anchors(abbrs, dict(zip(abbrs, ours)))
    A = np.array([anch[a] for a in abbrs])
    b, se, n_obs = gap_slope()
    snap = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    k_sum = sum(parts[a]["kalshi"] for a in abbrs)
    b_sum = sum(parts[a]["book"] for a in abbrs if parts[a]["book"] is not None)
    n_book = sum(parts[a]["book"] is not None for a in abbrs)
    print(f"overround: Kalshi team win means add up to {k_sum:.0f} and the {n_book} sportsbook lines to "
          f"{b_sum:.0f} (a season has exactly 1230 wins), so every anchor is lowered by "
          f"{(k_sum + b_sum * 30 / max(n_book, 1)) / 2 / 30 - 41:.2f} wins -- that alone favours 'under' sides.")

    # --- validation: the simulation must reproduce the bundle's own team means
    w0, _ = season.simulate(season.rating, 8000, 1)
    dev = np.abs(w0.mean(axis=0) - ours)
    print(f"price_futures snapshot {snap} | bank ${args.bank:,.0f} | sims {args.sims:,}")
    print(f"gap slope b = {b:.3f} +/- {se:.3f} over {n_obs} team-seasons "
          f"(share of our gap to the market that showed up in results)")
    print(f"validation: simulated vs bundle team wins, max miss {dev.max():.2f}, mean miss {dev.mean():.2f}"
          f"{'  <-- CHECK' if dev.max() > 0.5 else ''}")

    lam = {"b0": 0.0, "lo": max(b - se, 0.0), "mid": b, "hi": b + se}
    results, targets = {}, {}
    for sc, l in lam.items():
        tgt = A + l * (ours - A)
        scale = season.match_spread(residual_sd(l))
        base = season.calibrate(tgt, seed=7)
        wins, tr = season.simulate(base, args.sims, 11)
        flags = season.outcomes(wins, tr, 13)
        add_division_flags(flags, season, wins, 17)
        results[sc], targets[sc] = (wins, flags), tgt
        print(f"  scenario {sc:>3} (share of gap {l:.2f}): max |sim - target| = "
              f"{np.abs(wins.mean(axis=0) - tgt).max():.2f} wins; win SD {wins.std(axis=0).mean():.2f} "
              f"(backtest {residual_sd(l):.2f}, rating-uncertainty scale {scale:.2f})")

    # --- sanity checks on the simulated field (mid scenario)
    wins, flags = results["mid"]
    print("sanity (mid): title probs sum %.3f | conf champs %.3f | playoff teams %.2f (want 16) | "
          "play-in teams %.2f (want 8) | #1 seeds %.2f (want 2) | best %.3f worst %.3f" % (
              flags["champ"].mean(0).sum(), flags["conf"].mean(0).sum(), flags["made"].mean(0).sum(),
              flags["playin"].mean(0).sum(), flags["seed1"].mean(0).sum(), flags["best"].mean(0).sum(),
              flags["worst"].mean(0).sum()))

    print("\nTEAM WINS: ours vs market (calibrated = anchor + b * (ours - anchor))")
    print(f"{'team':4} {'ours':>6} {'Kalshi':>7} {'book':>6} {'anchor':>7} {'calib':>6} {'gap':>6}")
    order = np.argsort(-(ours - A))
    for i in order:
        a = abbrs[i]
        bk = parts[a]["book"]
        print(f"{a:4} {ours[i]:6.1f} {parts[a]['kalshi']:7.1f} {('%6.1f' % bk) if bk else '   n/a'} "
              f"{A[i]:7.1f} {targets['mid'][i]:6.1f} {ours[i] - A[i]:+6.1f}")

    # --- markets
    payload = fetch_markets(refresh=not args.no_fetch)
    rows = parse_markets(payload, abbrs)
    age = (time.time() - payload["fetched"]) / 60
    print(f"\nKalshi futures: {len(rows)} contracts from {len(KALSHI_SERIES)} series, quotes {age:.0f} min old")
    recs = price(rows, results, idx)

    def ok(c):
        two_sided = c["bid"] > 0 and c["ask"] > 0
        return (two_sided and (c["ask"] - c["bid"]) <= args.max_spread and c["depth_usd"] >= args.min_depth
                and c["ev"]["lo"] >= 0.02 and c["roi"]["lo"] >= args.min_roi)
    cands = sorted([c for c in recs if ok(c)], key=lambda c: -c["roi"]["lo"])
    print(f"{len(recs)} priced sides; {len(cands)} pass the filters (two-sided quote, spread <= "
          f"${args.max_spread:.2f}, >= ${args.min_depth:.0f} resting, ROI at b - 1 SE >= {args.min_roi:.0%})")
    print(LEGEND)

    def line(c, stake=None, k=None):
        mp = c["market_p"]
        out = (f"{c['label'][:34]:34} {c['side']:3} @{c['cost']:.2f} p(b){pct(c['p']['mid'])} "
               f"p(b-SE){pct(c['p']['lo'])} mkt{pct(mp)} EV(b){c['ev']['mid']:+.3f} EV(b-SE){c['ev']['lo']:+.3f} "
               f"ROI(b-SE){c['roi']['lo']:+5.0%} payout {c['multiple']:5.2f}x depth ${c['depth_usd']:6.0f}")
        return out if stake is None else out + f"  -> {k} contracts ${stake:6.2f}"

    print("\nTOP CANDIDATES (ranked by ROI at b - 1 SE)")
    for c in cands[:40]:
        print(line(c))

    if not cands:
        print("\nNo contract clears the filters: nothing to allocate.")
        return

    # --- size: expected-log-wealth optimum on the cautious scenario, then a fraction of it
    R = payoff_matrix(cands, results, idx, "lo")
    ub = np.array([min(args.per_pos, c["depth_usd"] / args.bank) for c in cands]) / args.frac
    x = optimise(cands, R, ub, args.per_team / args.frac, args.total / args.frac, args.frac)
    alloc = []
    for c, w in zip(cands, x):
        k = int(math.floor(w * args.bank / c["outlay"]))
        if k >= 3:
            alloc.append({**c, "contracts": k, "stake": k * c["outlay"]})
    alloc.sort(key=lambda c: -c["stake"])
    spent = sum(c["stake"] for c in alloc)
    print(f"\nALLOCATION  (quarter-Kelly on the b - 1 SE case; caps: {args.per_pos:.0%} per contract, "
          f"{args.per_team:.0%} per team, {args.total:.0%} total; whole contracts, fee included)")
    for c in alloc:
        print(line(c, c["stake"], c["contracts"]))
    print(f"deployed ${spent:,.2f} of ${args.bank:,.0f}; cash ${args.bank - spent:,.2f}; {len(alloc)} positions")

    # --- portfolio outcome under each assumption about how much of the gap is real
    by_kind = {}
    for c in alloc:
        key = "wins ladder" if c["kind"] == "wins" else c["kind"]
        by_kind[key] = by_kind.get(key, 0.0) + c["stake"]
    print("by market type: " + ", ".join(f"{k} ${v:,.0f}" for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])))
    pf_stats = {}
    if alloc:
        xs = np.array([c["stake"] for c in alloc])
        print("\nPORTFOLIO P&L over the simulated 2026-27 seasons (dollars, after fees)")
        print(f"{'if the real share of the gap is':34} {'mean':>8} {'median':>8} {'P(loss)':>8} {'5th pct':>8} {'95th pct':>9}")
        names = {"b0": "0 (market is right)", "lo": f"{lam['lo']:.2f} (b - 1 SE)", "mid": f"{lam['mid']:.2f} (b)",
                 "hi": f"{lam['hi']:.2f} (b + 1 SE)"}
        for sc in ("b0", "lo", "mid", "hi"):
            Rm = payoff_matrix(alloc, results, idx, sc)
            st = stats(Rm @ xs)
            pf_stats[sc] = st
            print(f"{names[sc]:34} {st['mean']:+8.1f} {st['median']:+8.1f} {st['p_loss']:8.0%} "
                  f"{st['p5']:+8.0f} {st['p95']:+9.0f}")

    print("\nLimits: b is a noisy estimate (n=%d team-seasons); the playoff-stage numbers (title, conference)"
          " are blended halfway to the market because that stage is not backtested; the sportsbook file is"
          " hand-copied; quotes move -- re-check the live order book and size before any trade. This is"
          " research output, not financial advice, and nothing here places an order." % n_obs)

    out = {"snapshot": snap, "bank": args.bank, "b": b, "se": se, "n": n_obs,
           "scenarios": {sc: float(l) for sc, l in lam.items()},
           "allocation": [{k: c[k] for k in ("ticker", "label", "side", "kind", "team", "cost", "fee", "p",
                                             "market_p", "contracts", "stake")} for c in alloc],
           "candidates": [{k: c[k] for k in ("ticker", "label", "side", "kind", "team", "cost", "p", "ev", "roi",
                                             "multiple", "depth_usd", "bid", "ask")} for c in cands],
           "portfolio": pf_stats}
    (PROC / "futures_pricing.json").write_text(json.dumps(out, indent=1, default=float))
    print(f"wrote {PROC / 'futures_pricing.json'}")


if __name__ == "__main__":
    main()
