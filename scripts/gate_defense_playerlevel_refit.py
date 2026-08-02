"""Gate (root-cause item): refit the box DEFENSIVE metric at the PLAYER level against PURE RAPM,
instead of the current team-level fit against team defensive rating.

Why: in nbaproj/impact.py build_impact the box def_impact is a ridge fit whose TARGET is TEAM
defensive rating -- which is exactly why defense ends up ~60% rebounds+blocks (team defense
correlates with rostering a rim-protecting center). The proposed fix (deep-dive item #4) refits the
defensive box coefficients at the PLAYER level against PURE RAPM def (data/processed/pure_rapm/,
2013-2025 -- PURE, not box-informed, to avoid circularity), optionally with TEAM FIXED EFFECTS, so
weights come from teammate-vs-teammate contrasts.

The scoping pre-check (2026-08-02) found: the mechanism WORKS at the coefficient level (dreb weight
collapses when you target player-level pure RAPM), BUT the apparent "win" is largely CIRCULAR --
def_rating_rel_z (an on/off number ~ a crude RAPM) alone carries R2 0.47 of the full 0.58, and the
honest BOX-ONLY refit (drop def_rating_rel_z) is weak (R2 0.13), barely moving the rebounding
correlation and NOT raising blocks/steals. So this gate tests the HONEST variants only.

Design (self-contained; does NOT modify build_impact or any tracked parquet):
- Load the shipped player_impact.parquet (has off_impact, the def z-features, team_id).
- Walk-forward, for each season S in 2016..2025: fit a poss-weighted ridge of PURE def_rapm on the
  BOX def z-features (dropping def_rating_rel_z -> the honest, non-circular set), using only prior
  RAPM seasons (2013..S-1); score season S players. Seasons 2013-2015 (too few prior RAPM seasons)
  keep the shipped def_impact (fallback).
- MOMENT-MATCH the refit def_impact to the shipped per-season mean/SD, so the gate isolates the
  RE-WEIGHTING (less rebounding) from a scale change and keeps scale consistent across refit and
  fallback seasons. Then impact = off_impact + def_impact_new.
- Two arms: NEW-honest (no FE) and NEW-honest-FE (team fixed effects, de-meaned within season-team).
- A/B through the identical 5000-sim walk-forward path as the shipped model. Cached box-informed RAPM
  prior is used for the blend's RAPM arm on both sides (conservative, matches the repo gate protocol).

    python scripts/gate_defense_playerlevel_refit.py
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
from sklearn.linear_model import Ridge  # noqa: E402

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
FIRST_RAPM, FIRST_REFIT = 2013, 2016   # need >=3 prior RAPM seasons before refitting
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}
RIDGE_ALPHA = 25.0

# Honest, non-circular defensive z-features: the box set MINUS def_rating_rel_z (the on/off number
# that makes the refit circular -- it is itself a crude RAPM).
BOX_DEF_Z = ["stl_p100_z", "blk_p100_z", "dreb_p100_z", "pf_p100_z",
             "rim_supp_z", "rim_vol_z", "rim_val_z", "defl_p36_z", "cont2_p36_z"]


def _load_pure_rapm() -> pd.DataFrame:
    frames = []
    for s in range(FIRST_RAPM, LAST + 1):
        p = PROC / "pure_rapm" / f"pure_rapm_{s}.parquet"
        if p.exists():
            d = pd.read_parquet(p)[["player_id", "def_rapm", "poss"]].rename(
                columns={"poss": "rapm_poss"}).copy()
            d["season_start"] = s
            frames.append(d)
    return pd.concat(frames, ignore_index=True)


def refit_def(old_imp: pd.DataFrame, feats: list[str], use_fe: bool) -> pd.DataFrame:
    """Player-level pure-RAPM refit of def_impact, moment-matched to the shipped per-season scale."""
    imp = old_imp.copy()
    pr = _load_pure_rapm()
    new_def = imp["def_impact"].astype(float).copy()

    for S in range(FIRST_REFIT, LAST + 1):
        train = imp[(imp.season_start >= FIRST_RAPM) & (imp.season_start < S)].merge(
            pr, on=["player_id", "season_start"], how="inner").dropna(subset=["def_rapm"])
        if train.season_start.nunique() < 3 or len(train) < 200:
            continue  # keep shipped def_impact (fallback)
        Xtr = train[feats].fillna(0.0).astype(float).copy()
        ytr = train["def_rapm"].astype(float).to_numpy()
        wtr = train["rapm_poss"].clip(lower=1.0).astype(float).to_numpy()
        if use_fe:  # team fixed effects: de-mean features + target within (season, team)
            key = train["season_start"].astype(str) + "_" + train["team_id"].astype(str)
            Xtr = Xtr - Xtr.groupby(key.to_numpy()).transform("mean")
            ytr = ytr - pd.Series(ytr, index=train.index).groupby(key.to_numpy()).transform("mean").to_numpy()
        model = Ridge(alpha=RIDGE_ALPHA, fit_intercept=not use_fe)
        model.fit(Xtr.to_numpy(), ytr, sample_weight=wtr)

        test = imp[imp.season_start == S]
        pred = model.predict(test[feats].fillna(0.0).astype(float).to_numpy())
        # moment-match to the shipped def_impact for season S (isolate re-weighting from scale).
        # nan-safe: the shipped def_impact is NaN for non-rotation players -- preserve that structure.
        old_s = test["def_impact"].astype(float).to_numpy()
        om, os_ = np.nanmean(old_s), np.nanstd(old_s)
        ps = pred.std()
        matched = om + (pred - pred.mean()) / (ps if ps > 1e-9 else 1.0) * os_
        matched = np.where(np.isnan(old_s), np.nan, matched)  # keep NaN where shipped had none
        new_def.loc[test.index] = matched

    imp["def_impact"] = new_def
    imp["impact"] = imp["off_impact"].astype(float) + imp["def_impact"]
    return imp


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
    print(f"{lbl:<30} MAE {ex.MAE.mean():.4f}  cov {ex['cov'].mean():.1%}")


def _delta(old_df, new_df, lbl):
    d = old_df.merge(new_df, on="season", suffixes=("_o", "_n"))
    d = d[~d.short_o]
    diff = d.MAE_o - d.MAE_n
    print(f"  {lbl:<28} delta(old-new) exShort {diff.mean():+.4f}  "
          f"SE {diff.std() / np.sqrt(len(diff)):.4f}  {(diff > 0).sum()}/{len(diff)} folds better")


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    pts, pgl, gl, ts, rosters, ages = _load_common()

    print("loading OLD impact (shipped, team-target def fit)...", flush=True)
    old_imp = pd.read_parquet(PROC / "player_impact.parquet")

    # coefficient diagnostic: how does the refit reweight defense vs the shipped correlation?
    for lbl, feats, fe in [("NEW-honest (box-only, no FE)", BOX_DEF_Z, False),
                           ("NEW-honest-FE (box-only, team FE)", BOX_DEF_Z, True)]:
        v = refit_def(old_imp, feats, fe)
        sc = v[v.season_start >= FIRST_REFIT]
        print(f"  {lbl}: corr(def_impact, dreb_p100) = "
              f"{sc['def_impact'].corr(sc['dreb_p100']):.3f}  (shipped "
              f"{old_imp[old_imp.season_start>=FIRST_REFIT]['def_impact'].corr(old_imp[old_imp.season_start>=FIRST_REFIT]['dreb_p100']):.3f})",
              flush=True)

    print("building NEW-honest (box-only, no FE)...", flush=True)
    a_imp = refit_def(old_imp, BOX_DEF_Z, False)
    print("building NEW-honest-FE (box-only, team FE)...", flush=True)
    b_imp = refit_def(old_imp, BOX_DEF_Z, True)

    print(f"\nGATE: player-level pure-RAPM def refit ({N_SIMS} sims, paired seeds)\n", flush=True)
    old_df = gate(old_imp, pts, pgl, gl, ts, rosters, ages)
    a_df = gate(a_imp, pts, pgl, gl, ts, rosters, ages)
    b_df = gate(b_imp, pts, pgl, gl, ts, rosters, ages)

    _summ("OLD (shipped)", old_df)
    _summ("NEW-honest (no FE)", a_df)
    _summ("NEW-honest-FE", b_df)
    print()
    _delta(old_df, a_df, "NEW-honest (no FE)")
    _delta(old_df, b_df, "NEW-honest-FE")
    print("\n(positive delta = NEW better. Shipped exShort baseline ~7.59 under this seed set.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
