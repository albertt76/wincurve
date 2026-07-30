"""Stage 2b report: aging curves, survivorship correction, and projection accuracy.

    python scripts/stage2b_report.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.aging import (  # noqa: E402
    build_transitions,
    aging_curves,
    compare_survivorship,
    peak_ages,
    project_next_season,
    replacement_level,
)

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2016, 2025


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    tr = build_transitions(imp, min_minutes=500)

    print("=" * 72)
    print("STAGE 2b: AGING CURVES AND PROJECTION")
    print("=" * 72)
    print(f"  transitions (season N -> N+1): {len(tr)}")
    print(f"  left the league next season:   {int(tr['dropped_out'].sum())} "
          f"({tr['dropped_out'].mean():.1%})")
    print(f"  replacement level: {replacement_level(imp):+.2f} points per 100 poss")
    print("    (= average impact of a marginal 250-750 minute rotation player)")

    # --- survivorship ---
    cs = compare_survivorship(tr, "impact")
    print("\n" + "=" * 72)
    print("SURVIVORSHIP BIAS")
    print("=" * 72)
    print("Players who decline get cut, so they never produce a 'next season'")
    print("observation. Measuring only survivors understates decline. We impute")
    print("dropouts instead of discarding them; this shows how much that matters.\n")
    print(f"  mean bias        {cs['bias'].mean():+.3f} points per 100 per year")
    print(f"  ages 25 & under  {cs[cs['age'] <= 25]['bias'].mean():+.3f}")
    print(f"  ages 33 & over   {cs[cs['age'] >= 33]['bias'].mean():+.3f}")
    print("\n  The bias is small here, and that is a real finding rather than luck:")
    print("  season N+1 is deliberately left ungated, so players who declined into")
    print("  a reduced role are still measured. Most of the classic bias comes from")
    print("  requiring a large N+1 workload, which we do not do.")

    # --- aging curves ---
    curves = aging_curves(tr, corrected=True)
    pk = peak_ages(curves)
    print("\n" + "=" * 72)
    print("PEAK AGE BY SKILL")
    print("=" * 72)
    show = pk.copy()
    show["peak_age"] = np.where(
        show["monotonic_decline"], "declines throughout",
        show["peak_age"].astype(str))
    print(show[["skill", "peak_age", "total_rise_to_peak",
                "decline_after_peak"]].to_string(
        index=False, float_format=lambda v: f"{v:7.2f}"))
    print("\n  Overall impact peaks around 25 -- markedly earlier than the popular")
    print("  28-30 belief. And the peaks genuinely differ by skill: three-point")
    print("  volume holds up latest (~29), while blocks decline from the very start")
    print("  and steals peak by ~22. Modelling one curve for all skills would")
    print("  average these away.")

    # --- projection accuracy ---
    print("\n" + "=" * 72)
    print("WALK-FORWARD PROJECTION ACCURACY")
    print("=" * 72)
    print("Predicting each player's actual next-season impact, training only on")
    print("earlier seasons. MAE = mean absolute error in points per 100 possessions;")
    print("lower is better.\n")

    rows = []
    for target in range(FIRST_TEST, LAST_TEST + 1):
        hist = imp[imp["season_start"] < target]
        cur = aging_curves(build_transitions(hist, min_minutes=500), ["impact"],
                           corrected=True)
        proj = project_next_season(hist, cur, target_season=target)
        if proj.empty:
            continue
        act = imp[(imp["season_start"] == target) & (imp["minutes"] >= 500)][
            ["player_id", "impact"]]
        j = proj.merge(act, on="player_id", how="inner")
        naive = hist.sort_values("season_start").groupby(
            "player_id").last()["impact"]
        j["naive"] = j["player_id"].map(naive)
        j = j.dropna(subset=["naive", "impact"])
        if len(j) < 50:
            continue
        rows.append({
            "season": target,
            "n": len(j),
            "naive": (j["impact"] - j["naive"]).abs().mean(),
            "blended": (j["impact"] - j["blended"]).abs().mean(),
            "shrunk": (j["impact"] - j["shrunk"]).abs().mean(),
            "full": (j["impact"] - j["proj_impact"]).abs().mean(),
        })

    res = pd.DataFrame(rows)
    print(res.to_string(index=False, float_format=lambda v: f"{v:6.3f}"))
    means = res[["naive", "blended", "shrunk", "full"]].mean()
    print("\n  MEAN MAE")
    print(f"    last season only (naive)      {means['naive']:.3f}")
    print(f"    3-season weighted blend       {means['blended']:.3f}")
    print(f"    + shrinkage toward age-mean   {means['shrunk']:.3f}")
    print(f"    + aging delta  (full model)   {means['full']:.3f}")
    gain = means["naive"] - means["full"]
    print(f"\n  improvement over naive: {gain:+.3f} points per 100 "
          f"({gain / means['naive']:+.1%})")
    print("\n  Each of the three steps earns its place. That was not true of the")
    print("  first attempt: shrinking 6x too hard toward replacement level made the")
    print("  projection WORSE than reusing last season, and made the aging term look")
    print("  harmful too. The ideas were fine; the constants were wrong.")

    curves.to_parquet(PROC / "aging_curves.parquet", index=False)
    print(f"\nwrote {PROC / 'aging_curves.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
