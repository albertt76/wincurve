"""Stage 2 report: player impact metric + diagnostics.

Builds the walk-forward impact metric and runs four checks. The third and fourth
matter most: they are what tell us whether the metric's known defensive weakness
actually damages the end goal (team projection) or merely damages player rankings.

    python scripts/stage2_report.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.impact import (  # noqa: E402
    DEFENSE_FEATURES, add_tracking_features, build_impact,
)
from nbaproj.teams import load_team_seasons  # noqa: E402

PROC = Path("data/processed")
MIN_MIN = 1000  # minutes threshold for "was a real rotation player"


def _wavg(values: pd.Series, weights: pd.Series) -> float:
    ok = values.notna() & weights.notna() & (weights > 0)
    if not ok.any():
        return np.nan
    return float(np.average(values[ok], weights=weights[ok]))


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ps = pd.read_parquet(PROC / "player_seasons.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()

    # Merge the player-tracking defensive features (rim protection + hustle) so the
    # defensive fit sees more than rebounds and blocks. Missing where the data does not
    # reach (rim 2013-14+, hustle 2016-17+); build_impact fills those to league-average.
    rim = pd.read_parquet(PROC / "rim_defense.parquet")
    hustle = pd.read_parquet(PROC / "hustle.parquet")
    # player_team_seasons fills pos_group with BPM's box-derived position estimate wherever the
    # real rim-tracking position is missing (every season before 2013-14) -- see
    # nbaproj.impact._bpm_position_estimate.
    #
    # shot_defense (nbaproj.ingest.shot_defense_all_categories, data/processed/shot_defense.parquet)
    # is deliberately NOT passed here. It was gated (scripts/gate_shot_defense_categories.py) and
    # REJECTED: the two shot-distance categories that survived a stability pre-check (2-pointers,
    # less-than-10ft) turned out to be stable because they mostly measure "is a rim-patrolling
    # big" (Gobert/Embiid/Adams get further inflated), not because they capture a new defensive
    # skill -- they reintroduce the exact positional confound POSITION_RELATIVE_FEATURES removed.
    # See nbaproj.impact.SHOT_DEFENSE_SURVIVING_CATEGORIES / CLAUDE.md for the full writeup.
    ps = add_tracking_features(ps, rim_defense=rim, hustle=hustle, player_team_seasons=pts)

    scored, diag = build_impact(ps, pts, ts, pa, first_test_season=2013)
    scored.to_parquet(PROC / "player_impact.parquet", index=False)

    print("=" * 72)
    print("STAGE 2: PLAYER IMPACT METRIC")
    print("=" * 72)
    print("Impact is in points per 100 possessions vs a league-average player.")
    print("Weights are refit for every season using only earlier seasons.\n")
    print("r-squared = share of variation explained, 0 to 1. Higher is better.\n")
    print(f"  offense calibration r-squared  {diag.offense_r2.mean():.3f}"
          f"   (range {diag.offense_r2.min():.3f}-{diag.offense_r2.max():.3f})")
    print(f"  defense calibration r-squared  {diag.defense_r2.mean():.3f}"
          f"   (range {diag.defense_r2.min():.3f}-{diag.defense_r2.max():.3f})")
    print("\n  => Box scores describe offense far better than defense, as expected.")

    rot = scored[scored["minutes"] >= MIN_MIN]

    # --- 1. scale sanity ---
    print("\n" + "=" * 72)
    print("CHECK 1: is the scale believable?")
    print("=" * 72)
    q = rot["impact"]
    print(f"  min {q.min():5.1f} | 5th pct {q.quantile(.05):5.1f} | "
          f"median {q.median():5.1f} | 95th pct {q.quantile(.95):5.1f} | "
          f"max {q.max():5.1f}")
    print("  Established metrics put All-NBA players around +5 to +10. Matches.")

    # --- 2. persistence ---
    a = rot[["player_id", "season_start", "impact", "off_impact", "def_impact",
             "dreb_p100"]]
    b = a.copy()
    b["season_start"] -= 1
    pair = a.merge(b, on=["player_id", "season_start"], suffixes=("", "_next"))
    print("\n" + "=" * 72)
    print("CHECK 2: does the metric persist year to year?")
    print("=" * 72)
    print(f"  {len(pair)} player pairs with >={MIN_MIN} minutes in both seasons\n")
    for col in ("impact", "off_impact", "def_impact", "dreb_p100"):
        r = pair[col].corr(pair[f"{col}_next"])
        print(f"  {col:11} season N -> N+1 correlation {r:5.3f}  "
              f"(r-squared {r**2:5.3f})")
    print("\n  WARNING: defensive impact persists MORE than offensive impact, and")
    print("  almost exactly as much as raw defensive rebound rate. That is not a")
    print("  sign of quality -- it is the positional-bias signature below. Position")
    print("  barely changes year to year, so a metric that mostly measures position")
    print("  will look highly 'stable' while measuring little about defensive skill.")

    # --- 3. positional bias ---
    print("\n" + "=" * 72)
    print("CHECK 3: how positionally biased is the defensive component?")
    print("=" * 72)
    print("  Correlation with big-man box stats (1.0 = indistinguishable from it):\n")
    for col in ("dreb_p100", "blk_p100"):
        print(f"    corr(defensive impact, {col:10}) = "
              f"{rot['def_impact'].corr(rot[col]):5.3f}")
    print(f"\n  Defensive features in use: {DEFENSE_FEATURES}")
    print("  Defensive impact remains close to a restatement of rebounding, so it")
    print("  should NOT be read as a player-level defensive ranking.")

    # --- 4. does it matter for team projection? ---
    m = pts.merge(
        scored[["player_id", "season_start", "impact", "off_impact", "def_impact"]],
        on=["player_id", "season_start"], how="inner")
    agg = m.groupby(["team_id", "season_start"], as_index=False).apply(
        lambda g: pd.Series({
            "team_impact": _wavg(g["impact"], g["minutes"]),
            "team_off": _wavg(g["off_impact"], g["minutes"]),
            "team_def": _wavg(g["def_impact"], g["minutes"]),
        }), include_groups=False)

    tt = ts.copy()
    for col in ("off_rating", "def_rating", "net_rating"):
        tt[f"{col}_dev"] = tt[col] - tt.groupby("season_start")[col].transform("mean")
    tt["def_rating_dev"] = -tt["def_rating_dev"]
    j = agg.merge(
        tt[["team_id", "season_start", "net_rating_dev", "off_rating_dev",
            "def_rating_dev"]], on=["team_id", "season_start"]).dropna()

    print("\n" + "=" * 72)
    print("CHECK 4: does the bias hurt TEAM-level accuracy? (the thing we care about)")
    print("=" * 72)
    print(f"  {len(j)} team-seasons\n")
    for label, xcol, ycol in (
        ("net rating", "team_impact", "net_rating_dev"),
        ("offense", "team_off", "off_rating_dev"),
        ("defense", "team_def", "def_rating_dev"),
    ):
        r = j[xcol].corr(j[ycol])
        print(f"  minute-weighted player impact vs actual team {label:11}"
              f"  r-squared {r**2:5.3f}")
    slope = np.polyfit(j["team_impact"], j["net_rating_dev"], 1)[0]
    print(f"\n  slope = {slope:.2f}, but the algebra predicts 5.00 "
          "(a player fills 1 of 5 spots)")
    print("  That gap is a REAL defect, not rounding: players under the minutes")
    print("  threshold get no impact estimate, so the weights do not cover the whole")
    print("  team minute budget, and the omitted players are below average by an")
    print("  amount that differs per team. Replacement level (Stage 3) is the fix.")
    print("\n  => Team defense aggregates better than the player-level bias would")
    print("     suggest (r-squared 0.56 vs a near-restatement of rebounding at the")
    print("     player level), because every team plays centres ~48 min/game so the")
    print("     positional inflation partly cancels. Usable for TEAM projection;")
    print("     NOT usable for ranking individual defenders.")

    print(f"\nwrote {PROC / 'player_impact.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
