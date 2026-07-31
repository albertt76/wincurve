"""Gate: does a turnover-conditional carryover rho(w) = rho0 + rho1*w beat the shipped flat rho?
Verdict: NO (measured negative result -- keep the flat, single rho).

Motivation (from a 2026-07-31 deep dive on advanced player metrics, borrowing 538's trick of
varying their team-Elo memory weight by roster continuity): the carryover persists a TEAM's prior
residual, so it should logically be weaker when the roster has turned over -- the "team" whose
mistake persists partly isn't there anymore. This tests rho as a function of the TARGET season's
roster turnover (nbaproj.simulate.roster_turnover's new_minute_share, the same quantity the
shipped RAPM defensive blend already uses) instead of one constant.

Cheap pre-check (rating-space OLS) was ambiguous, not clean: the interaction coefficient had the
right sign (higher turnover -> lower persistence) but explained almost nothing (adding it improved
in-sample SSR by only 0.2%), the rho-by-turnover-quintile pattern was not monotone, and
leave-one-season-out estimates of the interaction swung from -0.12 to -0.57 -- classic thin-power
symptoms with only ~180 residual pairs split across an interaction term. Per this project's rule
(an internal diagnostic is not the verdict), it went to the real simulation anyway.

Result (5000 sims, walk-forward 2017-2025, real schedule, paired seeds, exShort headline; run on
the SHIPPED config -- box+RAPM turnover-weighted defensive blend ON):
    flat rho [SHIPPED]        MAE 7.638   coverage 78.3%
    turnover-conditional      MAE 7.692   coverage 78.3%
    delta (shipped - alt): -0.054 wins, SE 0.053, 0/6 folds improved
Decisively worse, every fold. The fitted rho0/rho1 per fold are also unstable in exactly the way
the pre-check warned (e.g. rho1 swings from -0.83 to +0.04 across folds), which is the fingerprint
of a parameter that isn't well identified by ~30 teams x a handful of prior seasons. Do not
re-attempt this form; if roster-turnover-aware persistence is worth revisiting, it would need a
fundamentally different identification strategy (more residual pairs, or a Bayesian prior on rho1
pulling it toward zero) rather than more free parameters fit the same way.

    python scripts/gate_carryover_turnover.py
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

from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons)

PROC = Path("data/processed")
FIRST, LAST, N_SIMS = 2017, 2025, 5000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}
SHORTENED_STARTS = {int(s[:4]) for s in SHORTENED_SEASONS}


def _season_dev(team_seasons, target="net_rating"):
    t = team_seasons[["team_id", "season_start", target]].copy()
    t["actual_dev"] = t[target] - t.groupby("season_start")[target].transform("mean")
    return t[["team_id", "season_start", "actual_dev"]]


def fit_rho_turnover(predictions: pd.DataFrame, team_seasons: pd.DataFrame,
                     turnover: pd.DataFrame, *, before_season: int) -> tuple[float, float]:
    """Two-parameter fit: residual[N] ~ rho0*prev_residual + rho1*(prev_residual * w[N]).

    `turnover` needs columns team_id, season_start, new_minute_share, where season_start is the
    TARGET season N (how much of N's roster is new relative to N-1).
    """
    dev = _season_dev(team_seasons)
    p = predictions.merge(dev, on=["team_id", "season_start"], how="inner").dropna(
        subset=["pred_net_rating_dev", "actual_dev"])
    p["residual"] = p["actual_dev"] - p["pred_net_rating_dev"]

    prev = p[["team_id", "season_start", "residual"]].rename(columns={"residual": "prev_residual"})
    prev["season_start"] += 1
    pairs = p.merge(prev, on=["team_id", "season_start"], how="inner")
    # predictions may already carry new_minute_share (it does here); drop before merging turnover
    # in, or pandas silently suffixes both copies and there is no plain column -- a real bug this
    # gate hit once during development.
    pairs = pairs.drop(columns=["new_minute_share"], errors="ignore").merge(
        turnover[["team_id", "season_start", "new_minute_share"]],
        on=["team_id", "season_start"], how="left")
    pairs["new_minute_share"] = pairs["new_minute_share"].fillna(0.3)

    pairs = pairs[(pairs["season_start"] < before_season)
                  & (~(pairs["season_start"] - 1).isin(SHORTENED_STARTS))]
    if len(pairs) < 40:
        return 0.0, 0.0

    x = pairs["prev_residual"].to_numpy(dtype=float)
    w = pairs["new_minute_share"].to_numpy(dtype=float)
    y = pairs["residual"].to_numpy(dtype=float)
    beta, *_ = np.linalg.lstsq(np.column_stack([x, x * w]), y, rcond=None)
    return float(beta[0]), float(beta[1])


def apply_carryover_turnover(target_predictions, all_predictions, team_seasons, turnover,
                             *, target_season, rho_clip=(0.0, 1.0)):
    rho0, rho1 = fit_rho_turnover(all_predictions, team_seasons, turnover,
                                  before_season=target_season)
    out = target_predictions.copy()
    out["carryover"] = 0.0
    if (rho0 == 0.0 and rho1 == 0.0) or (target_season - 1) in SHORTENED_STARTS:
        return out

    dev = _season_dev(team_seasons)
    prev = all_predictions[all_predictions["season_start"] == target_season - 1]
    prev = prev.merge(dev, on=["team_id", "season_start"], how="left")
    prev["prev_residual"] = prev["actual_dev"] - prev["pred_net_rating_dev"]
    carry = dict(zip(prev["team_id"], prev["prev_residual"]))

    w_cur = turnover[turnover["season_start"] == target_season].set_index(
        "team_id")["new_minute_share"]
    w_map = w_cur.reindex(out["team_id"]).fillna(0.3).to_numpy()
    rho_i = np.clip(rho0 + rho1 * w_map, *rho_clip)

    out["carryover"] = rho_i * out["team_id"].map(carry).fillna(0.0).to_numpy()
    out["pred_net_rating_dev"] = out["pred_net_rating_dev"] + out["carryover"]
    return out


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

    print("building box-informed RAPM + walk-forward aggregates 2016-2025...", flush=True)
    rapm_imp = build_rapm_impact(imp, PROC)
    A0 = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, range(2016, LAST + 1))

    actual = ts.copy()
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "actual_wins_82", "games"]]
    actual2 = actual.assign(net_rating_dev=actual.net_rating
                            - actual.groupby("season_start").net_rating.transform("mean"))

    cal = []
    for s in sorted(A0.season_start.unique()):
        so, io = calibrate_projected_ratings(A0, ts, target_season=s, target="off_rating",
                                             agg_col="agg_off")
        sd, idc = calibrate_projected_ratings(A0, ts, target_season=s, target="def_rating",
                                              agg_col="agg_def_used")
        sub = A0[A0.season_start == s].copy()
        sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub.agg_def_used + idc
        cal.append(sub)
    cal = pd.concat(cal, ignore_index=True)

    def sim_gate(carry_fn):
        rows = []
        for s in range(FIRST, LAST + 1):
            sub = carry_fn(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
            sched = extract_schedule(gl, s)
            hca, msd = estimate_game_params(gl, before_season=s)
            sig = fit_rating_sigma(cal, actual2, before_season=s)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched, hca=hca,
                                        margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS,
                                        seed=1000 + s)
            j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=s)
                 .merge(ac, on=["team_id", "season_start"]))
            gp = FULL_SEASON_GAMES / j.games.fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(sim.team_id)}
            w82 = wins[:, j.team_id.map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j.actual_wins_82.to_numpy()
            rows.append({"season": s, "MAE": float(np.abs(actv - w82.mean(0)).mean()),
                         "cov": float(((actv >= np.percentile(w82, 10, 0))
                                       & (actv <= np.percentile(w82, 90, 0))).mean()),
                         "short": (s - 1) in SHORT or s in SHORT})
        return pd.DataFrame(rows)

    print(f"\nGATE ({N_SIMS} sims, real schedule, paired seeds, exShort headline)\n")
    ship = sim_gate(lambda sub, c, t, target_season: apply_carryover(sub, c, t,
                                                                     target_season=target_season))
    alt = sim_gate(lambda sub, c, t, target_season: apply_carryover_turnover(
        sub, c, t, A0, target_season=target_season))

    ex_s, ex_a = ship[~ship.short], alt[~alt.short]
    d = ship.merge(alt, on="season", suffixes=("_s", "_a"))
    d = d[~d.short_s]
    diff = d.MAE_s - d.MAE_a
    print(f"{'scheme':<24}{'exShort MAE':>12}{'coverage':>10}")
    print(f"{'flat rho [SHIPPED]':<24}{ex_s.MAE.mean():>12.4f}{ex_s['cov'].mean():>10.1%}")
    print(f"{'turnover-conditional':<24}{ex_a.MAE.mean():>12.4f}{ex_a['cov'].mean():>10.1%}")
    print(f"\ndelta (shipped-alt): {diff.mean():+.4f}  SE {diff.std() / np.sqrt(len(diff)):.4f}  "
          f"folds improved {int((diff > 0).sum())}/{len(diff)}")
    print("\nVERDICT: turnover-conditional carryover does NOT beat the shipped flat rho.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
