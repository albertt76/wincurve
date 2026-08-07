"""Pre-check: is there a real, measurable injury-recovery performance dip? (Track 3, step 1)

An external model review (2026-08) flagged that a returning star (the data/overrides/
injury_returns.json machinery in nbaproj.rosters) is restored straight to full pre-injury
form -- prior_mpg and proj_availability are set from his basis season with no discount for
the well-known first-season-back dip. Before building a fitted discount curve (step 2, which
would need its own real-sim gate, scripts/gate_carryover.py-style), this is the cheap
pre-check: does the raw historical data even show a dip worth modeling?

This is NOT a gate and does not touch the live pipeline -- purely a descriptive, walk-forward-
safe scan of REAL outcomes already in player_impact.parquet (every row there is retrospective:
that season's actual box stats scored with pre-season-N-fitted weights, not a forward
projection). Natural experiment: for every player-season with games < 0.5 * that season's team
game count ("injury-like"; team_games handles lockout/covid shortened seasons), find the most
recent PRIOR season with minutes >= nbaproj.rosters.MIN_HEALTHY_MINUTES ("basis") and the very
next season ("return"). Compare his ACTUAL return-season impact to his basis-season impact --
the gap is what "restore to basis level" (the current override's assumption) gets wrong, on
average, if anything.

Expect a small cohort (this project's history: ideas built on ~150-200-pair samples, e.g. the
carryover-turnover reshape, have repeatedly turned out too low-power to trust a second free
parameter) -- report the sample size honestly and let it, not intuition, decide whether step 2
is worth building.

    python scripts/precheck_injury_recovery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.rosters import MIN_HEALTHY_MINUTES  # noqa: E402
from nbaproj.teams import load_team_seasons  # noqa: E402

PROC = Path("data/processed")


def build_cohort(imp: pd.DataFrame, team_games_by_season: pd.Series) -> pd.DataFrame:
    imp = imp.sort_values(["player_id", "season_start"]).reset_index(drop=True)
    imp["team_games"] = imp["season_start"].map(team_games_by_season)
    imp["injury_like"] = imp["games"] < 0.5 * imp["team_games"]

    rows = []
    for pid, g in imp.groupby("player_id"):
        g = g.sort_values("season_start").reset_index(drop=True)
        for _, row in g[g["injury_like"]].iterrows():
            injury_season = int(row["season_start"])
            prior = g[(g["season_start"] < injury_season)
                      & (g["minutes"] >= MIN_HEALTHY_MINUTES)]
            if prior.empty:
                continue
            basis = prior.iloc[-1]
            ret = g[g["season_start"] == injury_season + 1]
            if ret.empty:
                continue
            ret = ret.iloc[0]
            rows.append({
                "player_id": pid,
                "player_name": row.get("player_name", ""),
                "basis_season": int(basis["season_start"]),
                "injury_season": injury_season,
                "return_season": injury_season + 1,
                "share_missed": 1.0 - float(row["games"]) / max(float(row["team_games"]), 1.0),
                "seasons_since_basis": injury_season + 1 - int(basis["season_start"]),
                "age_return": float(ret["age"]) if pd.notna(ret.get("age")) else np.nan,
                "basis_impact": float(basis["impact"]),
                "basis_off": float(basis["off_impact"]),
                "basis_def": float(basis["def_impact"]),
                "return_impact": float(ret["impact"]),
                "return_off": float(ret["off_impact"]),
                "return_def": float(ret["def_impact"]),
                "delta_impact": float(ret["impact"] - basis["impact"]),
                "delta_off": float(ret["off_impact"] - basis["off_impact"]),
                "delta_def": float(ret["def_impact"] - basis["def_impact"]),
            })
    return pd.DataFrame(rows)


def _mean_se(x: pd.Series) -> tuple[float, float]:
    x = x.dropna()
    if len(x) < 2:
        return float(x.mean()) if len(x) else float("nan"), float("nan")
    return float(x.mean()), float(x.std() / np.sqrt(len(x)))


def main() -> int:
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    ts = load_team_seasons()
    team_games_by_season = ts.groupby("season_start")["games"].median()

    cohort = build_cohort(imp, team_games_by_season)
    print("=" * 72)
    print("INJURY-RECOVERY PRE-CHECK (descriptive only, not a gate)")
    print("=" * 72)
    print(f"cohort size: {len(cohort)} player-seasons "
          f"(missed >50% of a season, healthy season on record before it, "
          f"and a next season observed)\n")

    if len(cohort) < 15:
        print("Sample too small for any bucket breakdown to be meaningful "
              "(this is itself the headline finding).")

    m, se = _mean_se(cohort["delta_impact"])
    mo, seo = _mean_se(cohort["delta_off"])
    md, sed = _mean_se(cohort["delta_def"])
    print(f"mean delta (return - basis), all: impact {m:+.2f} +/- {se:.2f} SE   "
          f"off {mo:+.2f} +/- {seo:.2f}   def {md:+.2f} +/- {sed:.2f}")
    print("(negative = return season underperforms the 'restore to basis' assumption, "
          "i.e. a real recovery drag the current override does not model)\n")

    print("by share of season missed:")
    for lo, hi, lbl in [(0.5, 0.75, "50-75%"), (0.75, 1.01, ">75%")]:
        sub = cohort[(cohort["share_missed"] >= lo) & (cohort["share_missed"] < hi)]
        m, se = _mean_se(sub["delta_impact"])
        print(f"  {lbl:<8} n={len(sub):<4} delta_impact {m:+.2f} +/- {se:.2f} SE")

    print("\nby seasons since basis (1 = very next season, the classic 'first year back'):")
    for k in sorted(cohort["seasons_since_basis"].unique()):
        sub = cohort[cohort["seasons_since_basis"] == k]
        m, se = _mean_se(sub["delta_impact"])
        print(f"  {k} season(s) later   n={len(sub):<4} delta_impact {m:+.2f} +/- {se:.2f} SE")

    print("\nby return-season age:")
    for lo, hi, lbl in [(0, 26, "<26"), (26, 30, "26-29"), (30, 99, "30+")]:
        sub = cohort[(cohort["age_return"] >= lo) & (cohort["age_return"] < hi)]
        m, se = _mean_se(sub["delta_impact"])
        print(f"  {lbl:<8} n={len(sub):<4} delta_impact {m:+.2f} +/- {se:.2f} SE")

    print("\nbiggest single-player drags (most negative delta_impact):")
    show = cohort.sort_values("delta_impact").head(10)[
        ["player_name", "basis_season", "return_season", "share_missed", "delta_impact"]]
    print(show.to_string(index=False))

    print("\nVerdict guide: proceed to step 2 (a fitted discount curve + real gate) only if the")
    print("all-cohort mean delta is negative, sizeable relative to its SE (not just a coin flip),")
    print("and shows a believable shape (worse for larger share_missed / fewer seasons since")
    print("basis). A small, noisy, or directionless result here means stop -- do not build a")
    print("gate on top of a pattern that is not there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
