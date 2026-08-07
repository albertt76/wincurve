"""Gate an OFFENSIVE RAPM blend end-to-end, walk-forward, holding the shipped defensive blend
fixed so the offense change is isolated.

An external model review (2026-08) flagged that RAPM (regularized adjusted plus-minus, from
play-by-play) is blended into the team DEFENSIVE aggregate only (nbaproj.rapm_blend, shipped) --
offense stays pure box, and even the shipped defensive blend is comparison-only at the player
level. Guard gravity / shot-creation is exactly what plus-minus sees and the box score does not,
which plausibly explains why two-time All-NBA Jalen Brunson projects 80th league-wide by wins.
Code-confirmed starting point: `fit_rapm` already computes `off_rapm` per player-season (same
estimator as `def_rapm`) and every cached rapm_<season>_a2000.parquet already has it -- it was
simply discarded downstream until `nbaproj.rapm.build_rapm_impact(..., which="off")` (added
alongside this gate) wired it up the same way `which="def"` already did.

Mirrors scripts/gate_rapm_blend.py's structure exactly, but blends OFFENSE:

    agg_off_used = (1 - w) * agg_off_box  +  w * agg_off_rapm

The defensive side is held at its shipped turnover-blend value (agg_def_used) throughout every
run, so any MAE change is attributable to the offense change alone -- the same isolation the
original defensive gate used against a pure-box baseline.

Weight sweep: unlike defense (where the turnover weight is already decided and shipped), the
right offensive weight is itself an open question -- the review's proposed mechanism ("weight
heaviest for high-usage creators") is not the same thing as roster turnover. Alongside the
turnover weight (trivially reusable) and flat constants, this sweeps `top_scorer_share`
(nbaproj.rapm_blend.top_scorer_share_weight: a team's top scorer's share of team total points,
built from player_team_seasons.pts already on disk -- a free, no-new-data proxy for reliance on
one high-usage creator).

    python scripts/gate_rapm_offense_blend.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates, top_scorer_share_weight  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season,
)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons,
)

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
N_SIMS = 5000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ts = load_team_seasons()
    ages = pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()

    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "net_rating_dev", "actual_wins_82", "games"]]

    seasons = range(FIRST_TEST - 1, LAST_TEST + 1)
    rapm_imp = build_rapm_impact(imp, PROC)                       # shipped def-swap
    rapm_off_imp = build_rapm_impact(imp, PROC, which="off")      # new off-swap
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, seasons,
                            rapm_off_imp=rapm_off_imp)

    tss = top_scorer_share_weight(pts, seasons)
    A = A.merge(tss, on=["team_id", "season_start"], how="left")
    A["top_scorer_share"] = A["top_scorer_share"].fillna(A["top_scorer_share"].median())

    def run(weight) -> pd.DataFrame:
        """weight: 'box', 'rapm', 'turnover', 'top_scorer_share', or a float -> offensive
        aggregate to calibrate. Defense is ALWAYS the shipped turnover-blended agg_def_used,
        so only the offense change is measured."""
        B = A.copy()
        if weight == "box":
            B["agg_off_use"] = B["agg_off"]
        elif weight == "rapm":
            B["agg_off_use"] = B["agg_off_rapm"]
        elif weight == "turnover":
            w = B["new_minute_share"].clip(0.0, 1.0)
            B["agg_off_use"] = (1 - w) * B["agg_off"] + w * B["agg_off_rapm"]
        elif weight == "top_scorer_share":
            w = B["top_scorer_share"]
            B["agg_off_use"] = (1 - w) * B["agg_off"] + w * B["agg_off_rapm"]
        else:
            w = float(weight)
            B["agg_off_use"] = (1 - w) * B["agg_off"] + w * B["agg_off_rapm"]
        cal = []
        for s in sorted(B["season_start"].unique()):
            so, io = calibrate_projected_ratings(B, ts, target_season=s,
                                                 target="off_rating", agg_col="agg_off_use")
            sd, idc = calibrate_projected_ratings(B, ts, target_season=s,
                                                  target="def_rating", agg_col="agg_def_used")
            sub = B[B["season_start"] == s].copy()
            sub["pred_net_rating_dev"] = so * sub["agg_off_use"] + io + sd * sub["agg_def_used"] + idc
            cal.append(sub)
        cal = pd.concat(cal, ignore_index=True)
        rows = []
        for s in range(FIRST_TEST, LAST_TEST + 1):
            sub = apply_carryover(cal[cal["season_start"] == s].copy(), cal, ts, target_season=s)
            sched = extract_schedule(gl, s)
            hca, msd = estimate_game_params(gl, before_season=s)
            sig = fit_rating_sigma(cal, ac, before_season=s)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched,
                                        hca=hca, margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS)
            j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=s)
                 .merge(ac, on=["team_id", "season_start"]))
            gp = FULL_SEASON_GAMES / j["games"].fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(sim["team_id"])}
            w82 = wins[:, j["team_id"].map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j["actual_wins_82"].to_numpy()
            rows.append({"season": s, "MAE": float(np.abs(actv - w82.mean(0)).mean()),
                         "cov": float(((actv >= np.percentile(w82, 10, 0))
                                       & (actv <= np.percentile(w82, 90, 0))).mean()),
                         "short": (s - 1) in SHORT or s in SHORT})
        return pd.DataFrame(rows)

    print("=" * 70)
    print(f"RAPM OFFENSIVE BLEND GATE  ({N_SIMS} sims/fold, roster mode + carryover, "
          f"defense fixed at shipped turnover blend)")
    print("=" * 70)
    print("MAE = mean absolute error in wins; delta = improvement over box-only offense.\n")
    base = run("box")
    base_ex = base[~base["short"]]["MAE"].mean()
    print(f"{'offensive aggregate':<28} {'MAE_all':>8} {'exShort':>8} {'delta':>7} {'cov':>6} folds+")
    variants = [("box only (baseline)", "box"), ("blend w=0.25", 0.25),
                ("blend w=0.50", 0.5), ("blend w=turnover", "turnover"),
                ("blend w=top_scorer_share", "top_scorer_share"),
                ("pure RAPM off (w=1)", "rapm")]
    results = {}
    for label, weight in variants:
        df = run(weight)
        results[weight] = df
        ex = df[~df["short"]]
        d = base.merge(df, on="season", suffixes=("_b", "_x"))
        improved = (d["MAE_b"] > d["MAE_x"]).sum()
        print(f"{label:<28} {df['MAE'].mean():8.3f} {ex['MAE'].mean():8.3f} "
              f"{base_ex - ex['MAE'].mean():+7.3f} {df['cov'].mean():6.0%} {improved}/{len(df)}")

    best_label, best_weight = max(
        ((lbl, w) for lbl, w in variants if w != "box"),
        key=lambda lw: base_ex - results[lw[1]][~results[lw[1]]["short"]]["MAE"].mean())
    best = results[best_weight]
    m = base.merge(best, on="season", suffixes=("_box", "_bl"))
    m["delta"] = m["MAE_box"] - m["MAE_bl"]
    ex = m[~m["short_box"]]
    se = ex["delta"].std() / np.sqrt(len(ex))
    print(f"\n  best variant ({best_label}), excl-short: {base_ex:.3f} -> "
          f"{ex['MAE_bl'].mean():.3f}  (delta {ex['delta'].mean():+.3f} +/- {se:.3f} SE, "
          f"{(m['delta'] > 0).sum()}/{len(m)} folds)")
    verdict = "SHIP" if ex["delta"].mean() > 0.05 else "borderline / do not ship"
    print(f"  => {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
