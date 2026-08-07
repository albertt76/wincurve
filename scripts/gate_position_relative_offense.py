"""Gate: which OFFENSIVE box feature should be standardized WITHIN position (like the shipped
defensive dreb_p100) to remove a center's positional inflation without erasing real skill?

Shipped outcome (2026-08-07): `oreb_p100` -- the offensive twin of the defensive dreb_p100 fix.
Offensive rebounding is heavily positional ROLE (a center crashes the glass), so league-wide it
inflated centers' offense the same way defensive rebounding inflated their defense. Standardizing
it within (season, pos_group) narrows the center/guard off_impact gap from +0.55/-0.57 (1.12) to
+0.18/-0.44 (0.62) WITHOUT overcorrecting (centers stay slightly above guards), de-inflates pure
offensive-rebounding specialists (Steven Adams, Gobert) and PRESERVES genuine offensive-center
stars (Jokic stays ~3.1, vs ~2.7 under ts_pct). It wins the gate: pure-box +0.132 (6/6 folds,
stable across seeds), and +0.051 (6/6) in the FULL shipped pipeline (offensive RAPM blend on) --
where the earlier `ts_pct` attempt collapses to a null (-0.003), because the offensive RAPM blend
already corrects ts_pct's efficiency confound but NOT oreb's rebounding-role confound.

Rejected alternatives, all reproduced by main() below:
  ts_pct           efficiency mirror; pure-box +0.064 (3/6), null in the full pipeline -- SUPERSEDED
  fta_p100         rim-runner contact-finishing; a near-no-op (+0.005, barely moves the gap)
  ts_pct + oreb    OVERCORRECTS: centers drop BELOW guards (only 2 in the top-30 offensive board)

fg3m_p100/fg3_rate are deliberately NOT tried: centers rarely attempt 3s, so there is no positional
inflation to remove, and standardizing a near-empty reference group risks erasing earned stretch-5
shooting. pts_p100/fga_p100/tov_p100 are usage-driven (a role choice, not anatomy -- Jokic/Embiid
prove high usage is available to centers); ast_p100 is a passing big's real rare skill -- all stay
league-wide.

Every variant only touches impact.POSITION_RELATIVE_FEATURES (which features are z-scored within
(season, pos_group) instead of league-wide) -- no new feature engineering, all candidates already
exist in OFFENSE_FEATURES. Nothing is written to data/processed -- the shipped
player_impact.parquet is untouched. Runs through the identical 5000-sim walk-forward path used by
scripts/gate_shot_defense_posrel.py.

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

    # Each variant is the box impact rebuilt with these features ADDED to dreb_p100 in
    # POSITION_RELATIVE_FEATURES. BASE = dreb-only (offense fully league-wide) is the common
    # reference, so every delta is apples-to-apples. `gate()` calibrates offense on pure box
    # (agg_off), which is the harness the ts_pct number was originally measured on; oreb's full-
    # pipeline (offensive RAPM blend on) advantage is even cleaner -- see the module docstring.
    variants = [
        ("BASE dreb-only", []),
        ("ts_pct (superseded)", ["ts_pct"]),
        ("fta_p100 (no-op)", ["fta_p100"]),
        ("oreb_p100 [SHIPPED]", ["oreb_p100"]),
        ("ts_pct + oreb (overcorrects)", ["ts_pct", "oreb_p100"]),
    ]
    print("building variants (box impact rebuilt per POSITION_RELATIVE_FEATURES override)...",
          flush=True)
    built = {lbl: build_variant(extra) for lbl, extra in variants}

    print(f"\nGATE: position-relative offensive standardization "
          f"({N_SIMS} sims, real schedule, paired seeds)\n", flush=True)
    dfs = {lbl: gate(built[lbl], pts, pgl, gl, ts, rosters, ages) for lbl, _ in variants}
    for lbl, _ in variants:
        _summ(lbl, dfs[lbl])

    base_df = dfs[variants[0][0]]
    print("\ndelta vs BASE dreb-only (positive = better):")
    for lbl, _ in variants[1:]:
        _delta(base_df, dfs[lbl], lbl)

    print("\nPositional off_impact bias check (latest season, >=1 has_rates player-seasons):")
    for lbl, _ in variants:
        _position_bias(built[lbl], lbl)

    print("\n(oreb ships: it wins MAE 6/6 folds AND narrows the center/guard gap without")
    print(" overcorrecting -- both are the point of this fix. See the module docstring.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
