"""Gate: does extending nearest-defender tracking from rim-only to all 6 LeagueDashPtDefend
shot-distance categories (DRAYMOND-style) fix the perimeter-containment gap? Verdict: NO --
REJECTED for a mechanistic reason worth recording (do not re-attempt without fixing it first).

From the 2026-07-31 advanced-metrics deep dive (DRAYMOND, FiveThirtyEight): our tracking defense
pulls only one of six available shot-distance categories from LeagueDashPtDefend ("Less Than
6Ft" -- the rim). The other five ("Overall", "3 Pointers", "2 Pointers", "Less Than 10Ft",
"Greater Than 15Ft") are the same endpoint, same ToS surface, same 2013-14+ availability, and
were proposed as the way to finally attack perimeter containment -- the box/tracking metric's
one remaining named weakness (few countable events for a guard who contests jump shots).

## Stage 1 (cheap): year-over-year player-level stability, all 5 categories

Suppression = expected-minus-allowed FG%, volume-gated at >=3 attempts/game both seasons. Result
(`scripts/precheck_shot_defense.py`-equivalent, see nbaproj/impact.py's SHOT_DEFENSE_* constants):

    category            YoY stability r^2   same-season team-def corr
    Overall                    0.041                0.822
    3 Pointers                 0.002                0.525
    2 Pointers                 0.106                0.733
    Less Than 10Ft             0.174                0.570
    Greater Than 15Ft          0.010                0.535

Exactly the zones that would have addressed PERIMETER shooting -- 3-pointers and >15ft jumpers --
have year-over-year stability indistinguishable from noise (r^2 0.002 and 0.010). This
corroborates rather than fixes "perimeter containment produces few countable events even with
tracking": Second Spectrum's nearest-defender labelling has no arm position or facing direction,
so a closeout on a jump shooter is a much noisier signal than a rim contest. "Overall" (r^2=0.041)
is dropped too -- it is dominated by whichever shots a player defends most, which for a rotation
big is mostly rim shots, so it likely duplicates rim_supp rather than adding information. Only
two zones survive: 2-pointers (r^2=0.106) and less-than-10ft (r^2=0.174).

## Stage 2 (real 5000-sim gate): the survivors are stable for the WRONG reason

Built `p2_val`/`lt10_val` (expected-minus-allowed x volume, the same recipe as rim_val) for the
two survivors and ran the authoritative gate. Result -- **decisively worse, not neutral**:

    OLD (shipped, rim+hustle+BPM-pos)   MAE 7.6275   coverage 78.3%
    NEW (+2 shot-def categories)        MAE 7.6816   coverage 78.9%
    delta (old-new) exShort: -0.054, SE 0.016 (~3.4 SE), only 1/6 folds improved

This is not noise -- it is a real, well-explained degradation. The biggest movers are Rudy
Gobert (5 of the top 8, all seasons, further inflated +1.0 to +1.4), Joel Embiid (+1.09), and
Steven Adams (+1.03) -- exactly the profile POSITION_RELATIVE_FEATURES was built to correct.
"2-pointers" and "less-than-10ft" defended are stable year-to-year precisely BECAUSE they mostly
measure "is a rim-patrolling big," not because they capture a genuinely new defensive skill --
and because these two features are NOT position-relative standardized (unlike dreb_p100), adding
them reintroduces the same positional confound the project already spent effort removing.

## What this means, and the one live follow-up

The stability pre-check alone was not a sufficient filter: a feature can be reliably measured and
still be a reliable measurement of the wrong thing (position, not skill). The untested follow-up
this suggests -- NOT attempted here, given the gate above is decisive enough to stop on -- is
adding `p2_val`/`lt10_val` to `POSITION_RELATIVE_FEATURES` (standardizing within position group,
exactly like `dreb_p100`) rather than league-wide, since the failure mode is identical. Whether
that would help is untested; it is not assumed to work just because the earlier dreb fix did.

The raw data pull (`nbaproj.ingest.shot_defense_all_categories`, wired into `pull_all()`,
`data/processed/shot_defense.parquet`, all 5 categories x 13 seasons) is kept -- it is a clean,
ToS-safe, already-cached dataset regardless of this feature's outcome. The `nbaproj.impact`
merge code (`SHOT_DEFENSE_SURVIVING_CATEGORIES`, `SHOT_DEFENSE_FEATURES`,
`add_tracking_features`'s `shot_defense` parameter) is also kept, following the codebase's
existing pattern for optional tracking inputs (rim/hustle) -- but `scripts/stage2_report.py`
deliberately does NOT pass it, so the shipped model does not use it.

    python scripts/gate_shot_defense_categories.py
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
from nbaproj.impact import add_tracking_features, build_impact  # noqa: E402
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


def build_with_shot_defense() -> pd.DataFrame:
    """The rejected variant: shipped features + the 2 surviving shot-defense categories."""
    ps = pd.read_parquet(PROC / "player_seasons.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rim = pd.read_parquet(PROC / "rim_defense.parquet")
    hustle = pd.read_parquet(PROC / "hustle.parquet")
    shot_def = pd.read_parquet(PROC / "shot_defense.parquet")
    ps = add_tracking_features(ps, rim_defense=rim, hustle=hustle, player_team_seasons=pts,
                              shot_defense=shot_def)
    scored, _ = build_impact(ps, pts, ts, pa, first_test_season=2013)
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


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    pts, pgl, gl, ts, rosters, ages = _load_common()

    print("loading OLD impact (shipped, no shot-defense categories)...", flush=True)
    old_imp = pd.read_parquet(PROC / "player_impact.parquet")
    print("building NEW impact (+2 surviving shot-defense categories)...", flush=True)
    new_imp = build_with_shot_defense()

    print(f"\nAUTHORITATIVE #3 GATE ({N_SIMS} sims, real schedule, paired seeds)\n")
    old_df = gate(old_imp, pts, pgl, gl, ts, rosters, ages)
    new_df = gate(new_imp, pts, pgl, gl, ts, rosters, ages)

    for lbl, df in [("OLD (shipped)", old_df), ("NEW (+shot-def)", new_df)]:
        ex = df[~df.short]
        print(f"{lbl:<20} MAE {ex.MAE.mean():.4f}  cov {ex['cov'].mean():.1%}")

    d = old_df.merge(new_df, on="season", suffixes=("_o", "_n"))
    d = d[~d.short_o]
    diff = d.MAE_o - d.MAE_n
    print(f"\ndelta (old-new) exShort: {diff.mean():+.4f}  SE {diff.std() / np.sqrt(len(diff)):.4f}"
          f"  {(diff > 0).sum()}/{len(diff)} folds improved")
    print("\nVERDICT: extending to these 2 shot-defense categories makes the model WORSE.")
    print("Rejected. Not wired into scripts/stage2_report.py's default pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
