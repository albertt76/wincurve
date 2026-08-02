"""Gate: does POSITION-RELATIVE standardizing the shot-defense features rescue them?

The DRAYMOND all-category shot-defense experiment (scripts/gate_shot_defense_categories.py) was
REJECTED (7.628 -> 7.682, -0.054) because p2_val/lt10_val were standardized LEAGUE-WIDE, which
reintroduced the "is a rim-patrolling big" confound (Gobert/Embiid/Adams inflated) -- exactly the
confound POSITION_RELATIVE_FEATURES was built to remove for dreb_p100. The one live follow-up that
script flagged: add p2_val/lt10_val to POSITION_RELATIVE_FEATURES so they are z-scored WITHIN
(season, pos_group) like dreb_p100, not league-wide.

The scoping pre-check (2026-08-02) confirmed the confound removal is as clean as the shipped
dreb_p100 fix -- corr(z, is_center): p2_val +0.40 -> -0.06, lt10_val +0.44 -> -0.06 -- and the
leaderboard becomes credible (13-14/15 centers league-wide -> 5/15 within position, guards/wings
surface). The tempering: lt10_val is highly redundant with the shipped rim features (corr +0.72/
+0.73 with rim_supp_z/rim_val_z) and with p2_val (+0.88), and standalone YoY stability is modest
(r2 ~0.13). So the honest expectation is aggregate-MAE-neutral with better per-player credibility,
and -- crucially -- it should NOT hurt the way the league-wide version did, since the confound is
gone and ridge absorbs the collinearity.

This gate A/Bs, through the identical 5000-sim walk-forward path, the shipped model against:
  NEW-A  p2_val alone, standardized within position   (the recommended primary; avoids lt10 dupe)
  NEW-B  p2_val + lt10_val, both within position       (does the second feature add anything?)

Both NEW variants pass shot_defense into add_tracking_features (the shipped stage2_report.py
deliberately omits it) and monkeypatch impact.SHOT_DEFENSE_FEATURES (which features enter the fit)
and impact.POSITION_RELATIVE_FEATURES (which are within-position standardized). Nothing is written
to data/processed -- the shipped player_impact.parquet is untouched.

Protocol note: like gate_shot_defense_categories.py, both arms use the CACHED box-informed RAPM
prior (rapm_*_a2000.parquet), which was fit on the shipped box def. That makes the RAPM arm of the
turnover blend conservative for the NEW variants (it partly overwrites the box change on
high-turnover teams). If a variant clears, regenerate build_rapm.py before shipping (documented
convention).

    python scripts/gate_shot_defense_posrel.py
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

import nbaproj.impact as impact_mod  # noqa: E402
from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.impact import add_tracking_features  # noqa: E402
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


def build_variant(shot_features: list[str], posrel_extra: list[str]) -> pd.DataFrame:
    """Rebuild the box impact with the given shot-defense features, standardized within position.

    ``shot_features`` = which {p2_val,lt10_val} enter the defensive fit (impact.SHOT_DEFENSE_FEATURES).
    ``posrel_extra`` = which of them are standardized WITHIN position (added to dreb_p100).
    """
    ps = pd.read_parquet(PROC / "player_seasons.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rim = pd.read_parquet(PROC / "rim_defense.parquet")
    hustle = pd.read_parquet(PROC / "hustle.parquet")
    shot_def = pd.read_parquet(PROC / "shot_defense.parquet")
    ps = add_tracking_features(ps, rim_defense=rim, hustle=hustle, player_team_seasons=pts,
                              shot_defense=shot_def)
    old_sdf = impact_mod.SHOT_DEFENSE_FEATURES
    old_pos = impact_mod.POSITION_RELATIVE_FEATURES
    try:
        impact_mod.SHOT_DEFENSE_FEATURES = list(shot_features)
        impact_mod.POSITION_RELATIVE_FEATURES = ["dreb_p100"] + list(posrel_extra)
        scored, _ = impact_mod.build_impact(ps, pts, ts, pa, first_test_season=2013)
    finally:
        impact_mod.SHOT_DEFENSE_FEATURES = old_sdf
        impact_mod.POSITION_RELATIVE_FEATURES = old_pos
    return scored


def _load_common():
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
    return pts, pgl, gl, ts, rosters, ages


def gate(imp: pd.DataFrame, pts, pgl, gl, ts, rosters, ages) -> pd.DataFrame:
    rapm_imp = build_rapm_impact(imp, PROC)
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, range(2016, LAST + 1))
    actual = ts.copy()
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "actual_wins_82", "games"]]
    actual2 = actual.assign(net_rating_dev=actual.net_rating
                            - actual.groupby("season_start").net_rating.transform("mean"))
    cal = []
    for s in sorted(A.season_start.unique()):
        so, io = calibrate_projected_ratings(A, ts, target_season=s, target="off_rating",
                                             agg_col="agg_off")
        sd, idc = calibrate_projected_ratings(A, ts, target_season=s, target="def_rating",
                                              agg_col="agg_def_used")
        sub = A[A.season_start == s].copy()
        sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub.agg_def_used + idc
        cal.append(sub)
    cal = pd.concat(cal, ignore_index=True)

    rows = []
    for s in range(FIRST, LAST + 1):
        sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
        sched = extract_schedule(gl, s)
        hca, msd = estimate_game_params(gl, before_season=s)
        sig = fit_rating_sigma(cal, actual2, before_season=s)
        sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched, hca=hca,
                                    margin_sd=msd, sigma_rating=sig, n_sims=N_SIMS, seed=1000 + s)
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


def _summ(lbl, df):
    ex = df[~df.short]
    print(f"{lbl:<28} MAE {ex.MAE.mean():.4f}  cov {ex['cov'].mean():.1%}")


def _delta(old_df, new_df, lbl):
    d = old_df.merge(new_df, on="season", suffixes=("_o", "_n"))
    d = d[~d.short_o]
    diff = d.MAE_o - d.MAE_n
    print(f"  {lbl:<26} delta(old-new) exShort {diff.mean():+.4f}  "
          f"SE {diff.std() / np.sqrt(len(diff)):.4f}  {(diff > 0).sum()}/{len(diff)} folds better")


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    pts, pgl, gl, ts, rosters, ages = _load_common()

    print("loading OLD impact (shipped, no shot-defense)...", flush=True)
    old_imp = pd.read_parquet(PROC / "player_impact.parquet")
    print("building NEW-A (p2_val alone, within position)...", flush=True)
    a_imp = build_variant(["p2_val"], ["p2_val"])
    print("building NEW-B (p2_val + lt10_val, within position)...", flush=True)
    b_imp = build_variant(["p2_val", "lt10_val"], ["p2_val", "lt10_val"])

    print(f"\nGATE: position-relative shot defense ({N_SIMS} sims, real schedule, paired seeds)\n",
          flush=True)
    old_df = gate(old_imp, pts, pgl, gl, ts, rosters, ages)
    a_df = gate(a_imp, pts, pgl, gl, ts, rosters, ages)
    b_df = gate(b_imp, pts, pgl, gl, ts, rosters, ages)

    _summ("OLD (shipped)", old_df)
    _summ("NEW-A (p2 posrel)", a_df)
    _summ("NEW-B (p2+lt10 posrel)", b_df)
    print()
    _delta(old_df, a_df, "NEW-A p2 posrel")
    _delta(old_df, b_df, "NEW-B p2+lt10 posrel")
    print("\n(positive delta = NEW better. Shipped exShort baseline ~7.63 under this seed set.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
