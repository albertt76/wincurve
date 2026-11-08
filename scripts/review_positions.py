"""Review live Kalshi season-long positions once the season is under way: mark them to market,
re-simulate the rest of the season from the standings, compare holding against selling, and price
new contracts against the same simulation.

Why this exists: price_futures.py prices from the preseason projection alone. After opening night
the useful questions change -- what are the positions worth now, does the model still like them
at today's price, and what does the model now think is mispriced -- and every answer needs the
season so far folded in.

How it works
  1. MARK TO MARKET at live Kalshi quotes: a YES exits at the bid, a NO at 1 - YES ask, each minus
     the 0.07 * p * (1 - p) taker fee. "mid" is the midpoint for reference.
  2. REST-OF-SEASON SIMULATION. The prior is price_futures.py's preseason construction -- each
     team's mean pulled from the market anchor toward ours by the measured share b -- built from
     the FROZEN preseason Kalshi snapshot (data/processed/market_2026_27_preseason.json), so live
     in-season prices never leak into the prior. Each team's rating is then
         w * SRS so far + (1 - w) * prior,      w = N / (N + 46.4)
     SRS (simple rating system) is the schedule-adjusted point margin per game; N is the average
     games played. 46.4 reproduces the gated in-season weight w(25) = 0.35
     (scripts/gate_inseason_model.py), but below 25 games w is an extrapolation, not a gate result,
     so the report also shows w = 0.10 and 0.30. Rating uncertainty shrinks by sqrt(1 - w). The
     real remaining schedule is simulated; games not yet on it (the NBA Cup-assigned pair) are
     played against a league-average side. Banked wins add back with no error.
  3. HOLD VS SELL. For each position: its liquidation value now vs its expected value at
     settlement under the model and under a "no skill" lens (the market's preseason means
     updated by the same results). Both lenses matter: if even the no-skill lens says holding is
     worth more, the case does not rest on our preseason view.
  4. NEW CONTRACTS priced on both sides with the same filters as price_futures.py, and a
     model-implied rebalance: one fractional-Kelly optimisation over "sell some of what you hold"
     and "buy something new" together, on the cautious scenario (b - 1 SE), so new buys are sized
     against the risk you already carry.
  5. NEWS GUARD. In-season, a price can move on information the model never sees (an injury, a
     trade). The no-skill lens -- the market's own preseason view plus the same results -- is the
     check: when any liquid win-ladder or playoff contract of a team is priced more than 10 points
     from it, something besides results moved the market on that team, and every contract on the
     team is tagged NEWS? and kept out of the rebalance (sells and buys) unless --allow-news. Check
     the injury report before reading anything into them. The rebalance also only sells a holding
     when its bid beats the model's central probability, so it does not churn on a coin flip.

Honest limits: w below 25 games is extrapolated; the model knows nothing about in-season injuries,
trades or tanking beyond what the results already show; quotes are a snapshot and books are thin.
This is research output: it places no orders.

    python scripts/review_positions.py                        # positions_placed_2026-10-20.json
    python scripts/review_positions.py --positions data/processed/<file>.json --no-refresh
Positions file: price_futures.py's JSON output with a per-position "filled" (contracts actually
filled; defaults to "contracts") -- personal data, so it lives in gitignored data/processed/.
Output: a printed report and data/processed/positions_review.json (gitignored).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from nba_api.stats.endpoints import leaguegamelog, scheduleleaguev2  # noqa: E402
from scipy.optimize import minimize  # noqa: E402
from scipy.special import erf  # noqa: E402

import price_futures as pf  # noqa: E402
from nbaproj import ingest  # noqa: E402
from nbaproj.cache import cached_fetch  # noqa: E402
from nbaproj.inseason import PER100, _srs  # noqa: E402

PROC = ROOT / "data" / "processed"
PRESEASON_MARKET = PROC / "market_2026_27_preseason.json"
LIVE_QUOTES = pf.RAW / "kalshi_futures_27_live.json"
SEASON = "2026-27"
W25 = 0.35                              # gated in-season weight at N = 25 (docs/nba/shipped.md)
K_W = 25 * (1 - W25) / W25              # w(N) = N / (N + K_W)
SQRT2 = math.sqrt(2.0)
NEWS_GAP = 0.10                         # |price - no-skill probability| that suggests news

LEGEND = """Legend: SRS (simple rating system -- a team's point margin per game, adjusted for opponents);
w (how much the season so far counts against the preseason prior, 0 to 1); EV (expected value --
the average outcome if the model's probability is right); "sell" is what the position fetches now
at the bid after Kalshi's fee; "hold" is its expected payout at settlement; p(model) uses our
preseason view, p(no-skill) the market's preseason view, both updated by the same results."""


# ------------------------------------------------------------------------------- season state
def season_state(season: pf.Season, refresh: bool) -> dict:
    """Results so far and the remaining schedule, in the Season's team order."""
    gl = cached_fetch("team_game_log", leaguegamelog.LeagueGameLog,
                      {"season": SEASON, "season_type_all_star": "Regular Season",
                       "player_or_team_abbreviation": "T", "timeout": ingest.TIMEOUT}, refresh=refresh)
    sch = cached_fetch("schedule_league_v2", scheduleleaguev2.ScheduleLeagueV2,
                       {"season": SEASON, "league_id": "00", "timeout": ingest.TIMEOUT}, refresh=refresh)
    if gl.empty:
        raise SystemExit(f"no {SEASON} games yet -- use price_futures.py before opening night")
    ix = {t: i for i, t in enumerate(season.ids)}
    if set(gl.TEAM_ID.astype(int)) != set(ix):
        raise SystemExit("game log teams do not match the bundle's 30 TEAM_IDs")
    g = gl[["GAME_ID", "TEAM_ID", "PLUS_MINUS", "WL"]].copy()
    g = g.merge(g[["GAME_ID", "TEAM_ID"]].rename(columns={"TEAM_ID": "opp_id"}), on="GAME_ID")
    g = g[g.TEAM_ID != g.opp_id]
    srs = _srs(g)
    ids = season.ids
    obs = np.array([srs[t] * PER100 for t in ids])
    banked = np.array([int((gl[gl.TEAM_ID == t].WL == "W").sum()) for t in ids])
    played = np.array([int((gl.TEAM_ID == t).sum()) for t in ids])

    done = set(gl.GAME_ID.astype(str))
    rs = sch[sch.gameId.astype(str).str.startswith("002") & (sch.gameStatus != 3)
             & (sch.homeTeam_teamId != 0) & (sch.awayTeam_teamId != 0)]
    rs = rs[~rs.gameId.astype(str).isin(done)]
    home = np.array([ix[int(t)] for t in rs.homeTeam_teamId])
    away = np.array([ix[int(t)] for t in rs.awayTeam_teamId])
    hca_g = np.where(rs.isNeutral.to_numpy(bool), 0.0, season.hca)
    known = np.bincount(home, minlength=30) + np.bincount(away, minlength=30)
    extra = 82 - played - known                 # NBA Cup-assigned games not yet scheduled
    if extra.min() < 0:
        raise SystemExit("more than 82 games per team on the schedule -- check the schedule pull")
    G = len(home)
    H = np.zeros((G, 30), np.float32)
    A = np.zeros((G, 30), np.float32)
    H[np.arange(G), home] = 1
    A[np.arange(G), away] = 1
    return {"obs": obs, "banked": banked, "played": played, "home": home, "away": away,
            "hca_g": hca_g, "H": H, "A": A, "extra": extra, "through": str(gl.GAME_DATE.max())[:10],
            "first": str(gl.GAME_DATE.min())[:10]}


def sim_rest(season: pf.Season, st: dict, rate: np.ndarray, sig: np.ndarray, n: int, seed: int,
             chunk: int = 4000):
    """Final wins (banked + simulated remainder) and the drawn true ratings."""
    rng = np.random.default_rng(seed)
    wins = np.empty((n, 30), np.int16)
    tr = np.empty((n, 30), np.float32)
    emax = int(max(st["extra"].max(), 1))
    mask = (np.arange(emax)[None, :] < st["extra"][:, None]).astype(np.float32)
    for c0 in range(0, n, chunk):
        m = min(chunk, n - c0)
        t = rate[None, :] + rng.standard_normal((m, 30)) * sig[None, :]
        em = t[:, st["home"]] - t[:, st["away"]] + st["hca_g"][None, :]
        p = 0.5 * (1.0 + erf(em / (season.msd * SQRT2)))
        hw = (rng.random(em.shape) < p).astype(np.float32)
        w = hw @ st["H"] + (1.0 - hw) @ st["A"]
        pe = 0.5 * (1.0 + erf(t / (season.msd * SQRT2)))       # vs a league-average side
        w += ((rng.random((m, 30, emax)) < pe[:, :, None]) * mask[None, :, :]).sum(axis=2)
        wins[c0:c0 + m] = np.rint(st["banked"][None, :] + w).astype(np.int16)
        tr[c0:c0 + m] = t
    return wins, tr


def scenarios(season: pf.Season, st: dict, w: float, n: int) -> tuple[dict, dict]:
    """Rest-of-season worlds keyed like price_futures.py (b0, lo, mid, hi), plus the w
    sensitivity of the central case. Also returns the preseason calibrated means."""
    b, se, _ = pf.gap_slope()
    anch, _parts = pf.anchors(season.abbr, None, market_path=PRESEASON_MARKET)
    A = np.array([anch[a] for a in season.abbr])
    lam = {"b0": 0.0, "lo": max(b - se, 0.0), "mid": b, "hi": b + se}
    out, pre = {}, {}
    for sc, l in lam.items():
        target = A + l * (season.model_mean - A)
        season.match_spread(pf.residual_sd(l))
        base = season.calibrate(target, seed=7)
        pre[sc] = target
        for tag, ww in ((sc, w), (f"{sc}@0.10", 0.10), (f"{sc}@0.30", 0.30)):
            if tag != sc and sc not in ("mid", "b0"):
                continue
            rate = ww * st["obs"] + (1 - ww) * base
            sig = season.sigma * season.sigma_scale * math.sqrt(1 - ww)
            wins, tr = sim_rest(season, st, rate, sig, n, 11)
            flags = season.outcomes(wins, tr, 13)
            pf.add_division_flags(flags, season, wins, 17)
            out[tag] = (wins, flags)
    return out, {"lam": lam, "pre": pre, "b": b, "se": se}


# ---------------------------------------------------------------------------------- positions
def contract_fields(ticker: str, kind: str) -> int | None:
    return int(ticker.split("-")[-1]) if kind in ("wins", "maxwins") else None


def hits(c: dict, wins: np.ndarray, flags: dict, idx: dict) -> np.ndarray:
    if c["kind"] == "wins":
        h = wins[:, idx[c["team"]]] >= c["k"]
    elif c["kind"] == "maxwins":
        h = flags["max_wins"] >= c["k"]
    else:
        h = flags[c["kind"]][:, idx[c["team"]]]
    return h if c["side"] == "YES" else ~h


def load_positions(path: Path) -> tuple[list[dict], float]:
    d = json.loads(path.read_text())
    out = []
    for a in d["allocation"]:
        filled = float(a.get("filled", a["contracts"]))
        fee = pf.FEE_RATE * a["cost"] * (1 - a["cost"])
        out.append({**a, "k": contract_fields(a["ticker"], a["kind"]), "filled": filled,
                    "basis": filled * (a["cost"] + fee)})
    return out, float(d.get("bank", 0.0))


def quote_book(payload: dict) -> dict:
    book = {}
    for events in payload["series"].values():
        for e in events:
            for m in e.get("markets", []):
                book[m["ticker"]] = m
    return book


def exit_price(p: dict, m: dict | None) -> tuple[float, float]:
    """(price you get selling now, mid) for the position's side."""
    if m is None:
        return 0.0, float("nan")
    bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
    if p["side"] == "YES":
        return bid, (bid + ask) / 2 if (bid > 0 and ask > 0) else bid
    return (1.0 - ask) if ask > 0 else 0.0, 1.0 - (bid + ask) / 2 if (bid > 0 and ask > 0) else 0.0


def blend_playoff(p: float, c: dict, mid: float | None) -> float:
    """Same rule as price_futures.py: the playoff stage is not backtested."""
    if c["kind"] in pf.PLAYOFF_KINDS and mid is not None and not math.isnan(mid):
        return pf.PLAYOFF_BLEND * mid + (1 - pf.PLAYOFF_BLEND) * p
    return p


# ---------------------------------------------------------------------------------- rebalance
def rebalance(positions, cands, results, idx, cash, wealth, args):
    """Fractional-Kelly over sells of current holdings and new buys together, on the cautious
    (b - 1 SE) worlds. Fractional Kelly is implemented as full Kelly on a bankroll scaled by
    1 / frac, which keeps current holdings in the same units as new bets."""
    wins, flags = results["lo"]
    n_p, n_c = len(positions), len(cands)
    held = np.column_stack([hits(p, wins, flags, idx) for p in positions]).astype(float)
    settle = cash + held @ np.array([p["filled"] for p in positions])
    # per contract sold: cash now minus the payout given up
    gain = np.column_stack([p["exit"] - p["exit_fee"] - held[:, i] for i, p in enumerate(positions)])
    gain *= np.array([p["filled"] for p in positions])[None, :]
    can_sell = np.array([1.0 if p["exit"] > 0 else 0.0 for p in positions])
    R = pf.payoff_matrix(cands, results, idx, "lo") * wealth if n_c else np.zeros((len(settle), 0))
    K = (1.0 / args.frac - 1.0) * wealth
    M = np.hstack([gain, R])

    def f(z):
        W = K + settle + M @ z
        if W.min() <= 1e-6:
            return 1e12, np.zeros_like(z)
        return -np.log(W).mean(), -(M / W[:, None]).mean(axis=0)

    teams = sorted({c["team"] for c in positions + cands if c.get("team")})
    cons = [{"type": "ineq", "fun": lambda z: cash / wealth - z[n_p:].sum(),
             "jac": lambda z: np.r_[np.zeros(n_p), -np.ones(n_c)]}]
    for t in teams:
        a_s = np.array([p["basis"] if p.get("team") == t else 0.0 for p in positions]) / wealth
        a_b = np.array([1.0 if c.get("team") == t else 0.0 for c in cands])
        cap = args.per_team - a_s.sum()
        if cap < 0:      # already over the team cap: no new buys there, but never a forced sale
            a_s, cap = np.zeros(n_p), 0.0
        cons.append({"type": "ineq", "fun": lambda z, a_s=a_s, a_b=a_b, cap=cap: cap + a_s @ z[:n_p] - a_b @ z[n_p:],
                     "jac": lambda z, a_s=a_s, a_b=a_b: np.r_[a_s, -a_b]})
    held_val = np.array([p["value_exit"] for p in positions]) / wealth
    cons.append({"type": "ineq", "fun": lambda z: args.total - held_val.sum() + held_val @ z[:n_p] - z[n_p:].sum(),
                 "jac": lambda z: np.r_[held_val, -np.ones(n_c)]})
    beats = np.array([1.0 if p["exit"] - p["exit_fee"] > p["p_now"]["mid"] else 0.0 for p in positions])
    can_sell = can_sell * beats
    if not args.allow_news:
        can_sell = can_sell * np.array([0.0 if p["news"] else 1.0 for p in positions])
    ub = [(0.0, s) for s in can_sell] + [(0.0, min(args.per_pos, c["depth_usd"] / wealth)) for c in cands]
    res = minimize(f, np.zeros(n_p + n_c), jac=True, method="SLSQP", bounds=ub, constraints=cons,
                   options={"maxiter": 500, "ftol": 1e-12})
    z = np.clip(res.x, 0.0, None)
    return z[:n_p], z[n_p:] * wealth


# --------------------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--positions", type=Path, default=PROC / "positions_placed_2026-10-20.json")
    ap.add_argument("--bank", type=float, default=0.0, help="starting bankroll (default: the file's)")
    ap.add_argument("--sims", type=int, default=20000)
    ap.add_argument("--no-refresh", action="store_true", help="use cached results, schedule, quotes")
    ap.add_argument("--frac", type=float, default=0.25, help="fraction of full Kelly")
    ap.add_argument("--per-pos", type=float, default=0.06, help="max share of bankroll per new contract")
    ap.add_argument("--per-team", type=float, default=0.12, help="max share of bankroll on one team")
    ap.add_argument("--total", type=float, default=0.60, help="max share of bankroll in positions")
    ap.add_argument("--allow-news", action="store_true", help="let NEWS? contracts into the rebalance")
    ap.add_argument("--max-spread", type=float, default=0.06)
    ap.add_argument("--min-depth", type=float, default=10.0)
    ap.add_argument("--min-roi", type=float, default=0.10)
    args = ap.parse_args()
    if not PRESEASON_MARKET.exists():
        raise SystemExit(f"missing {PRESEASON_MARKET}: copy the last PRESEASON market_2026_27.json "
                         "there (an in-season copy would leak results into the prior)")

    positions, bank = load_positions(args.positions)
    bank = args.bank or bank
    teams = json.loads((PROC / "projections_current.json").read_text())["teams"]
    season = pf.Season(teams)
    ab, idx = season.abbr, {a: i for i, a in enumerate(season.abbr)}
    st = season_state(season, refresh=not args.no_refresh)
    N = float(st["played"].mean())
    w = N / (N + K_W)
    results, info = scenarios(season, st, w, args.sims)
    payload = pf.fetch_markets(refresh=not args.no_refresh, path=LIVE_QUOTES)
    book = quote_book(payload)
    snap = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")

    print(f"review_positions snapshot {snap} | results through {st['through']} | "
          f"avg games played {N:.1f} | w = {w:.2f} (also shown at 0.10 / 0.30)")
    print(f"positions: {args.positions.name} ({len(positions)}) | starting bank ${bank:,.0f}")
    print(LEGEND)

    # --- teams
    wins_m, flags_m = results["mid"]
    wins_0 = results["b0"][0]
    kal = json.loads(PRESEASON_MARKET.read_text())["teams"]
    held_teams = {p.get("team") for p in positions}
    print(f"\nTEAMS  (preseason Kalshi / our calibrated preseason mean / now: model, no-skill)")
    print(f"{'team':4} {'W-L':>6} {'SRS':>6} {'Kalshi':>7} {'calib':>6} {'model':>6} {'no-skill':>9}")
    for i in np.argsort(-wins_m.mean(axis=0)):
        a = ab[i]
        print(f"{a:4} {st['banked'][i]:>2}-{st['played'][i] - st['banked'][i]:<3} {st['obs'][i]:6.1f} "
              f"{float(kal[a]['wins']):7.1f} {info['pre']['mid'][i]:6.1f} {wins_m[:, i].mean():6.1f} "
              f"{wins_0[:, i].mean():9.1f} {'*' if a in held_teams else ''}")

    # --- news guard: teams whose prices moved beyond what results explain
    rows = pf.parse_markets(payload, ab)
    recs = pf.price(rows, {k: results[k] for k in ("b0", "lo", "mid", "hi")}, idx)
    news_teams = sorted({r["team"] for r in recs
                         if r["team"] and r["kind"] not in pf.PLAYOFF_KINDS and r["market_p"] is not None
                         and r["bid"] > 0 and r["ask"] > 0 and r["ask"] - r["bid"] <= args.max_spread
                         and abs(r["p"]["b0"] - r["market_p"]) > NEWS_GAP})
    print(f"\nNEWS? teams (a liquid contract sits >{NEWS_GAP:.0%} from the no-skill probability): "
          f"{', '.join(news_teams) or 'none'}")

    # --- positions: mark to market and hold vs sell
    for p in positions:
        m = book.get(p["ticker"])
        p["exit"], p["mid"] = exit_price(p, m)
        p["exit_fee"] = pf.FEE_RATE * p["exit"] * (1 - p["exit"])
        p["value_exit"] = p["filled"] * max(p["exit"] - p["exit_fee"], 0.0)
        p["value_mid"] = p["filled"] * (0.0 if math.isnan(p["mid"]) else p["mid"])
        mid_yes = None if m is None else (float(m.get("yes_bid_dollars") or 0) + float(m.get("yes_ask_dollars") or 0)) / 2
        mid_side = None if mid_yes is None else (mid_yes if p["side"] == "YES" else 1 - mid_yes)
        p["p_now"] = {tag: blend_playoff(float(hits(p, *results[tag], idx).mean()), p, mid_side)
                      for tag in results}
        p["hold_model"] = p["filled"] * p["p_now"]["mid"]
        p["hold_noskill"] = p["filled"] * p["p_now"]["b0"]
        p["news"] = p.get("team") in news_teams
    basis = sum(p["basis"] for p in positions)
    v_exit = sum(p["value_exit"] for p in positions)
    v_mid = sum(p["value_mid"] for p in positions)
    print("\nPOSITIONS  (sorted by profit at the bid)")
    print(f"{'position':30} {'side':4} {'filled':>7} {'paid':>8} {'sell now':>9} {'P&L':>8} "
          f"{'p entry':>7} {'p model':>7} {'w .10-.30':>10} {'p no-skill':>10} {'hold':>8} {'hold-sell':>9}")
    for p in sorted(positions, key=lambda p: p["value_exit"] - p["basis"]):
        pn = p["p_now"]
        print(f"{p['label'][:30]:30} {p['side']:4} {p['filled']:7.1f} {p['basis']:8.2f} {p['value_exit']:9.2f} "
              f"{p['value_exit'] - p['basis']:+8.2f} {p['p']['mid']:7.0%} {pn['mid']:7.0%} "
              f"{pn['mid@0.10']:4.0%}-{pn['mid@0.30']:<4.0%} {pn['b0']:10.0%} {p['hold_model']:8.2f} "
              f"{p['hold_model'] - p['value_exit']:+9.2f}{'  NEWS?' if p['news'] else ''}")
    print(f"TOTAL paid ${basis:,.2f} | sell everything now ${v_exit:,.2f} ({v_exit - basis:+,.2f}) | "
          f"at mid ${v_mid:,.2f} ({v_mid - basis:+,.2f})")
    print("hold-sell > 0: the model expects more from holding to settlement than selling now; < 0: the "
          "price already exceeds the model's expectation. NEWS?: the price is more than "
          f"{NEWS_GAP:.0%} from the no-skill probability, so the market likely knows something the model doesn't.")

    pnl_stats = {}
    for tag, name in (("mid", "model"), ("b0", "no-skill")):
        wins, flags = results[tag]
        pnl = sum(np.where(hits(p, wins, flags, idx), p["filled"], 0.0) for p in positions) - basis
        pnl_stats[name] = {"mean": float(pnl.mean()), "p_profit": float((pnl > 0).mean()),
                           "p5": float(np.percentile(pnl, 5)), "p95": float(np.percentile(pnl, 95))}
        print(f"held to settlement ({name}): mean {pnl.mean():+,.0f}, chance of profit {(pnl > 0).mean():.0%}, "
              f"5th-95th pct {np.percentile(pnl, 5):+,.0f} to {np.percentile(pnl, 95):+,.0f}")

    # --- new contracts
    held_keys = {(p["ticker"], p["side"]) for p in positions}
    opposite = {(t, "NO" if s == "YES" else "YES") for t, s in held_keys}

    def ok(c):
        two_sided = c["bid"] > 0 and c["ask"] > 0
        return (two_sided and (c["ask"] - c["bid"]) <= args.max_spread and c["depth_usd"] >= args.min_depth
                and c["ev"]["lo"] >= 0.02 and c["roi"]["lo"] >= args.min_roi
                and (c["ticker"], c["side"]) not in opposite)
    cands = sorted([c for c in recs if ok(c)], key=lambda c: -c["roi"]["lo"])
    for c in cands:
        c["news"] = (c["team"] in news_teams) if c["team"] else abs(c["p"]["b0"] - c["market_p"]) > NEWS_GAP
    print(f"\nNEW CONTRACTS: {len(recs)} priced sides, {len(cands)} pass the filters "
          f"(ROI at b - 1 SE >= {args.min_roi:.0%}; opposite sides of holdings excluded)")
    for c in cands[:25]:
        held = " (adds to a holding)" if (c["ticker"], c["side"]) in held_keys else ""
        print(f"  {c['label'][:32]:32} {c['side']:3} @{c['cost']:.2f} p(model){c['p']['mid']:6.0%} "
              f"p(b-SE){c['p']['lo']:6.0%} p(no-skill){c['p']['b0']:6.0%} mkt{c['market_p']:5.0%} "
              f"ROI(b-SE){c['roi']['lo']:+5.0%} payout {c['multiple']:5.2f}x depth ${c['depth_usd']:,.0f}{held}"
              f"{'  NEWS?' if c['news'] else ''}")
    n_news = sum(c["news"] for c in cands)
    print(f"  {n_news} of {len(cands)} are NEWS? -- the market has moved on those teams beyond what results explain")
    pool = cands if args.allow_news else [c for c in cands if not c["news"]]

    # --- model-implied rebalance
    cash = bank - basis
    wealth = cash + v_exit
    sell_frac, buy_usd = rebalance(positions, pool, results, idx, cash, wealth, args)
    print(f"\nMODEL-IMPLIED REBALANCE  (fractional Kelly {args.frac:.2f} on the b - 1 SE worlds, current "
          f"wealth ${wealth:,.0f} = cash ${cash:,.0f} + positions at the bid; NEWS? contracts "
          f"{'included' if args.allow_news else 'excluded'})")
    sells = []
    for p, u in zip(positions, sell_frac):
        k = math.floor(u * p["filled"])
        if k >= 1:
            sells.append({"ticker": p["ticker"], "label": p["label"], "side": p["side"], "contracts": k,
                          "of": p["filled"], "price": p["exit"], "proceeds": k * (p["exit"] - p["exit_fee"])})
            print(f"  SELL {k:5d} of {p['filled']:7.1f}  {p['label'][:30]:30} {p['side']:3} @{p['exit']:.2f} "
                  f"-> ${k * (p['exit'] - p['exit_fee']):,.2f}")
    buys = []
    for c, x in zip(pool, buy_usd):
        k = math.floor(x / c["outlay"])
        if k >= 3:
            buys.append({"ticker": c["ticker"], "label": c["label"], "side": c["side"], "contracts": k,
                         "price": c["cost"], "stake": k * c["outlay"]})
            print(f"  BUY  {k:5d}  {c['label'][:30]:30} {c['side']:3} @{c['cost']:.2f} -> ${k * c['outlay']:,.2f}")
    if not sells and not buys:
        print("  no change: the current holdings are where the model wants them")
    print("\nLimits: w below 25 games is extrapolated; the model sees in-season injuries, trades and tanking"
          " only through results; the playoff-stage numbers are blended halfway to the market; quotes are a"
          " snapshot and books are thin -- check the live order book. Research output: nothing here places"
          " an order.")

    out = {"news_teams": news_teams, "snapshot": snap, "through": st["through"], "N": N, "w": w, "b": info["b"], "se": info["se"],
           "totals": {"paid": basis, "sell_now": v_exit, "mid": v_mid}, "held_to_settlement": pnl_stats,
           "positions": [{k: p[k] for k in ("ticker", "label", "side", "filled", "basis", "exit", "mid",
                                             "value_exit", "p_now", "hold_model", "hold_noskill", "news")}
                         for p in positions],
           "candidates": [{k: c[k] for k in ("ticker", "label", "side", "cost", "p", "roi", "multiple",
                                             "depth_usd", "news")} for c in cands],
           "rebalance": {"sells": sells, "buys": buys}}
    (PROC / "positions_review.json").write_text(json.dumps(out, indent=1, default=float))
    print(f"wrote {PROC / 'positions_review.json'}")


if __name__ == "__main__":
    main()
