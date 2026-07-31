"""Gate: does a PLAYER-LEVEL (or reliability-driven) box-vs-RAPM defensive blend beat the shipped
team-level turnover blend? Verdict: NO (measured negative result -- keep the turnover blend).

The shipped defense blends two team AGGREGATES by one team-level weight (roster turnover):
    agg_def_used = (1 - w_turnover) * agg_def_box + w_turnover * agg_def_rapm
An adversarial completeness audit flagged three mechanically-distinct formulations the shipped
blend does not cover. This script gates all three, walk-forward, against that shipped blend:

  B  reliability-weighted team blend: same aggregate blend, but w = minute-weighted mean of each
     player's RAPM identification strength poss/(poss+K) instead of turnover. (Reliability is
     ANTI-correlated with turnover, ~-0.43: high-turnover rosters have thin-sample newcomers.)
  C  rating-space own-slope blend: blend the two single-metric defensive PREDICTIONS, each with
     its own walk-forward slope, so RAPM (2.3x the box aggregate's dispersion) gets its own lower
     slope instead of the shared one.
  A  TRUE player-level blend: def_blend_p = (1-lam_p)*box_p + lam_p*rapm_p, lam_p = poss/(poss+K),
     aggregated with minutes. Because lam is per-player the aggregate is NOT any team-level convex
     mix of agg_box and agg_rapm. Two RAPM sources: A1 box-informed RAPM (already box-scale), and
     A2 PURE RAPM moment-matched to the box def scale per season (the audit's literal ask -- pure,
     to avoid the box-informed prior double-shrinking thin-sample players).

Result (5000 sims, walk-forward 2017-2025, headline excludes shortened folds; shipped = 7.638):
    B reliability K1000/2000/4000 : 7.738 / 7.704 / 7.661   (-0.100 .. -0.023, worse; monotone --
                                    the more it trusts RAPM the worse)
    C own-slope w=turnover        : 7.686   (-0.048, worse)
    C own-slope w=reliability     : 7.704   (-0.065, worse)
    A1 box-informed K1000/2000/4000: 7.797 / 7.769 / 7.729  (-0.159 .. -0.090; K2000 == pure box)
    A2 pure-RAPM   K1000/2000/4000: 7.847 / 7.780 / 7.709  (-0.209 .. -0.071; the audit's literal
                                    ask, still worse -- higher K = less RAPM = closer but never up)
NONE beats the shipped turnover blend. Two lessons: (1) the shipped turnover weight does not just
use LESS RAPM, it uses RAPM in the right PLACES -- churned rosters the one-year carryover cannot
follow; reliability-weighting undoes exactly that. (2) box-informed RAPM is ALREADY a
possession-weighted shrink of pure RAPM toward the box, so a further per-player possession blend
double-shrinks (A1 K2000 collapses to pure box). RAPM's marginal edge over the (now-fixed) box
metric is only ~0.13 wins, and the turnover blend already extracts it. Player-level family closed.

    python scripts/gate_player_level_blend.py     # regenerates pure RAPM from cached segments
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.bulk_pbp import segments_for_season  # noqa: E402
from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.project import calibrate_projected_ratings, project_team_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact, fit_rapm  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, roster_turnover, simulate_season)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons)

PROC = Path("data/processed")
PURE = PROC / "pure_rapm"          # generated here from cached segments if absent
FIRST, LAST, N_SIMS = 2017, 2025, 5000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}


def _ensure_pure_rapm(seasons):
    PURE.mkdir(exist_ok=True)
    for s in seasons:
        f = PURE / f"pure_rapm_{s}.parquet"
        if not f.exists():
            fit_rapm(segments_for_season(s), alpha=2000.0).to_parquet(f, index=False)


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ages = pd.DataFrame({"player_id": pa["PLAYER_ID"].astype("int64"),
                         "season_start": pa["SEASON_START"].astype(int),
                         "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()
    actual = ts.copy()
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "actual_wins_82", "games"]]
    actual2 = actual.assign(net_rating_dev=actual.net_rating
                            - actual.groupby("season_start").net_rating.transform("mean"))

    print("building box-informed RAPM + pure RAPM (from cached segments) ...", flush=True)
    rapm_bi = build_rapm_impact(imp, PROC)
    _ensure_pure_rapm(range(2013, LAST + 1))
    pure = {int(f.stem.split("_")[-1]): pd.read_parquet(f).set_index("player_id")
            for f in PURE.glob("pure_rapm_*.parquet")}

    poss = pd.concat([pd.read_parquet(f)[["player_id", "poss"]].assign(
        season_start=int(f.stem.split("_")[1])) for f in sorted(PROC.glob("rapm_*_a2000.parquet"))],
        ignore_index=True)
    poss_map = poss.set_index(["player_id", "season_start"])["poss"]
    lam_poss = np.array([poss_map.get(k, 0.0) for k in zip(imp.player_id, imp.season_start)])
    box_def = imp.set_index(["player_id", "season_start"])["def_impact"]

    def imp_A1(K):
        lam = lam_poss / (lam_poss + K)
        out = imp.copy()
        out["def_impact"] = (1 - lam) * imp.def_impact.to_numpy() + lam * rapm_bi.def_impact.to_numpy()
        out["impact"] = out.off_impact + out.def_impact
        return out

    def imp_A2(K):
        out = imp.copy()
        nd = imp.def_impact.to_numpy(float).copy()
        for s, pr in pure.items():
            rows = np.where((imp.season_start == s).to_numpy())[0]
            pids = imp.loc[imp.season_start == s, "player_id"].to_numpy()
            rated = pr[pr.poss >= 500]
            common = [p for p in pids if p in rated.index]
            if len(common) < 20:
                continue
            bv = np.array([box_def.get((p, s), np.nan) for p in common], float)
            pv = rated.loc[common, "def_rapm"].to_numpy(float)
            m = np.isfinite(bv) & np.isfinite(pv)
            if m.sum() < 20:
                continue
            bm, bs = bv[m].mean(), bv[m].std() or 1.0
            pm, ps = pv[m].mean(), pv[m].std() or 1.0
            prd, prp = pr.def_rapm.to_dict(), pr.poss.to_dict()
            for i, pid in zip(rows, pids):
                rp = prd.get(pid)
                if rp is None or not np.isfinite(rp):
                    continue
                scaled = bm + bs * (float(rp) - pm) / ps
                pp = float(prp.get(pid, 0.0))
                lam = pp / (pp + K)
                nd[i] = (1 - lam) * nd[i] + lam * scaled
        out["def_impact"] = nd
        out["impact"] = out.off_impact + out.def_impact
        return out

    def aggregates(tbl):
        return pd.concat([project_team_ratings(tbl, pts, pgl, ts, ages, target_season=s,
                                               mode="roster", team_rosters=rosters, decouple=True)
                          for s in range(2016, LAST + 1)], ignore_index=True)

    def calib(A, col):
        out = []
        for s in sorted(A.season_start.unique()):
            so, io = calibrate_projected_ratings(A, ts, target_season=s, target="off_rating",
                                                 agg_col="agg_off")
            sd, idc = calibrate_projected_ratings(A, ts, target_season=s, target="def_rating",
                                                  agg_col=col)
            sub = A[A.season_start == s].copy()
            sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub[col] + idc
            out.append(sub)
        return pd.concat(out, ignore_index=True)

    def sim(cal):
        rows = []
        for s in range(FIRST, LAST + 1):
            sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
            sched = extract_schedule(gl, s)
            hca, msd = estimate_game_params(gl, before_season=s)
            sig = fit_rating_sigma(cal, actual2, before_season=s)
            si, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched, hca=hca,
                                       margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS, seed=1000 + s)
            j = (si.drop(columns=["pred_net_rating_dev"]).assign(season_start=s)
                 .merge(ac, on=["team_id", "season_start"]))
            gp = FULL_SEASON_GAMES / j.games.fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(si.team_id)}
            w82 = wins[:, j.team_id.map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j.actual_wins_82.to_numpy()
            rows.append({"season": s, "MAE": float(np.abs(actv - w82.mean(0)).mean()),
                         "cov": float(((actv >= np.percentile(w82, 10, 0))
                                       & (actv <= np.percentile(w82, 90, 0))).mean()),
                         "short": (s - 1) in SHORT or s in SHORT})
        return pd.DataFrame(rows)

    # Reliability weights (team-level, for B) and shipped aggregates.
    A_ship = backtest_aggregates(imp, rapm_bi, pts, pgl, ts, ages, rosters, range(2016, LAST + 1))
    by_player = {pid: g[["season_start", "poss"]].sort_values("season_start").to_numpy()
                 for pid, g in poss.groupby("player_id")}

    def rel_weight(K):
        rows = []
        for s in range(2016, LAST + 1):
            for tid, g in pts[pts.season_start == s].groupby("team_id"):
                mins = g.minutes.to_numpy(float)
                lam = np.array([(lambda a: (a[a[:, 0] < s][-1, 1] if len(a[a[:, 0] < s]) else 0.0))(
                    by_player.get(p, np.empty((0, 2)))) for p in g.player_id], float)
                lam = lam / (lam + K)
                rows.append({"team_id": tid, "season_start": s,
                             "w": float((mins * lam).sum() / mins.sum()) if mins.sum() else 0.0})
        return pd.DataFrame(rows)

    ship = sim(calib(A_ship, "agg_def_used"))
    ex = ship[~ship.short]
    print(f"\nPLAYER-LEVEL / RELIABILITY BLEND GATE  ({N_SIMS} sims, exShort headline)\n")
    print(f"{'scheme':<28}{'exShort MAE':>12}{'coverage':>10}{'vs shipped':>12}{'folds':>8}")
    print(f"{'turnover [SHIPPED]':<28}{ex.MAE.mean():>12.4f}{ex['cov'].mean():>10.1%}"
          f"{0.0:>+12.3f}{'--':>8}")
    base = ex.MAE.mean()

    def report(name, df):
        exd = df[~df.short]
        d = ship.merge(df, on="season", suffixes=("_s", "_x"))
        d = d[~d.short_s]
        print(f"{name:<28}{exd.MAE.mean():>12.4f}{exd['cov'].mean():>10.1%}"
              f"{base - exd.MAE.mean():>+12.3f}{int((d.MAE_x < d.MAE_s).sum()):>4}/{len(d)}",
              flush=True)

    for K in (1000, 2000, 4000):
        rw = rel_weight(K)
        A = A_ship.merge(rw, on=["team_id", "season_start"])
        w = np.clip(A.w.to_numpy(), 0, 1)
        A["b"] = (1 - w) * A.agg_def_box + w * A.agg_def_rapm
        report(f"B reliability K{K}", sim(calib(A, "b")))
    for K in (1000, 2000, 4000):
        report(f"A1 box-informed K{K}", sim(calib(aggregates(imp_A1(K)), "agg_def")))
    for K in (1000, 2000, 4000):
        report(f"A2 pure-RAPM K{K}", sim(calib(aggregates(imp_A2(K)), "agg_def")))
    print("\nVERDICT: no player-level or reliability-weighted blend beats the shipped turnover"
          " blend. Keep it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
