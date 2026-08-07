"""Gate: does position-relative standardizing OFFENSIVE efficiency fix center-offense inflation?

An external model review (2026-08) flagged that position-relative standardization was applied to
defensive rebounding (dreb_p100, shipped, see POSITION_RELATIVE_FEATURES) but never to the
offensive side, even though the same mechanism plausibly applies: a low-usage center's near-100%
efficiency at the rim is opportunity/role, not shot-making skill the way a guard's efficiency is.
Data-confirmed symptom on the 2026-27 projection: centers average +0.64 off_impact vs guards -0.18
(11 of the top 30 players by projected wins are centers), and specific low-usage bigs (Walker
Kessler 8th league-wide by wins, Jalen Duren, Daniel Gafford, Ryan Kalkbrenner, Neemias Queta) get
near-star offensive credit.

This gate A/Bs, through the identical 5000-sim walk-forward path used by
scripts/gate_shot_defense_posrel.py, the shipped model against:
  NEW-A  ts_pct alone, standardized within position         (the primary candidate)
  NEW-B  ts_pct + oreb_p100, both within position            (does the second feature add anything?)

fg3m_p100/fg3_rate are deliberately NOT tried here: centers rarely attempt 3s, so there is no
positional inflation to remove, and standardizing within a near-empty reference group risks
manufacturing noise or erasing genuinely earned skill (stretch-5 shooting). pts_p100/fga_p100/
tov_p100 are usage-driven (a role choice, not anatomy -- Jokic/Embiid prove high usage is
available to centers) and are also left league-wide. ast_p100 is left out of this first pass: a
passing big (Jokic, Sabonis) is real rare skill and position-relative treatment could amplify
rather than remove it.

Both NEW variants only touch impact.POSITION_RELATIVE_FEATURES (which features are z-scored
within (season, pos_group) instead of league-wide) -- no new feature engineering, ts_pct and
oreb_p100 already exist in the standard OFFENSE_FEATURES set. Nothing is written to
data/processed -- the shipped player_impact.parquet is untouched.

Position-group dependency note: pos_group comes from rim_defense.PLAYER_POSITION (2013-14+),
falling back to the BPM box-derived position estimate (scripts/gate_bpm_position.py) for the 8
pre-2013-14 backbone seasons. That fallback was validated for defensive rebounding; this gate
inherits it for offense without a separate check of its own.

    python scripts/gate_position_relative_offense.py
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


def build_variant(posrel_extra: list[str]) -> pd.DataFrame:
    """Rebuild the box impact with the given offensive features added to
    POSITION_RELATIVE_FEATURES (standardized within (season, pos_group) instead of league-wide).
    """
    ps = pd.read_parquet(PROC / "player_seasons.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rim = pd.read_parquet(PROC / "rim_defense.parquet")
    hustle = pd.read_parquet(PROC / "hustle.parquet")
    ps = add_tracking_features(ps, rim_defense=rim, hustle=hustle, player_team_seasons=pts)
    old_pos = impact_mod.POSITION_RELATIVE_FEATURES
    try:
        impact_mod.POSITION_RELATIVE_FEATURES = ["dreb_p100"] + list(posrel_extra)
        scored, _ = impact_mod.build_impact(ps, pts, ts, pa, first_test_season=2013)
    finally:
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


def _position_bias(imp: pd.DataFrame, lbl: str) -> None:
    """Spot-check: does the positional off_impact gap actually shrink?"""
    latest = imp[imp.season_start == imp.season_start.max()]
    if "pos_group" not in latest.columns:
        return
    g = latest[latest.has_rates].groupby(latest["pos_group"].fillna("F"))["off_impact"].mean()
    print(f"  {lbl:<26} mean off_impact by position: " +
          "  ".join(f"{k}={v:+.2f}" for k, v in g.items()))


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    pts, pgl, gl, ts, rosters, ages = _load_common()

    print("loading OLD impact (shipped, offense league-wide)...", flush=True)
    old_imp = pd.read_parquet(PROC / "player_impact.parquet")
    print("building NEW-A (ts_pct alone, within position)...", flush=True)
    a_imp = build_variant(["ts_pct"])
    print("building NEW-B (ts_pct + oreb_p100, within position)...", flush=True)
    b_imp = build_variant(["ts_pct", "oreb_p100"])

    print(f"\nGATE: position-relative offensive standardization "
          f"({N_SIMS} sims, real schedule, paired seeds)\n", flush=True)
    old_df = gate(old_imp, pts, pgl, gl, ts, rosters, ages)
    a_df = gate(a_imp, pts, pgl, gl, ts, rosters, ages)
    b_df = gate(b_imp, pts, pgl, gl, ts, rosters, ages)

    _summ("OLD (shipped)", old_df)
    _summ("NEW-A (ts_pct posrel)", a_df)
    _summ("NEW-B (ts_pct+oreb posrel)", b_df)
    print()
    _delta(old_df, a_df, "NEW-A ts_pct posrel")
    _delta(old_df, b_df, "NEW-B ts_pct+oreb posrel")

    print("\nPositional off_impact bias check (latest season, >=1 has_rates player-seasons):")
    _position_bias(old_imp, "OLD (shipped)")
    _position_bias(a_imp, "NEW-A ts_pct posrel")
    _position_bias(b_imp, "NEW-B ts_pct+oreb posrel")

    print("\n(positive delta = NEW better. Ship only if MAE improves with a believable SE AND the")
    print(" center/guard off_impact gap visibly narrows -- both are the point of this fix.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
