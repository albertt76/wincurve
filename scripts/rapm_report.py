"""Validate single-season RAPM against the box-score defensive metric.

Answers the question that motivates the whole RAPM effort: does adjusted plus/minus capture
team defense that the box score cannot see?

Three checks, in order of how much they prove:
  1. Face validity -- are the top defensive-RAPM players actually known elite defenders?
  2. Divergence -- how different is RAPM defense from the box-score defensive component? If
     they agree, RAPM adds nothing; the box-score bias (correlates 0.83 with rebound rate)
     predicts they should differ.
  3. Team reconstruction -- aggregated to team level, which better matches actual team
     defensive rating? (In-sample and partly circular for one season, but informative. The
     clean cross-season predictive test needs a second season -- see --predict.)

    python scripts/rapm_report.py --season 2024
    python scripts/rapm_report.py --season 2024 --alpha 2000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.rapm import fit_rapm  # noqa: E402
from nbaproj.teams import load_team_seasons  # noqa: E402

PROC = Path("data/processed")
MIN_POSS = 500  # players below this get too little signal to rank


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--alpha", type=float, default=2000.0)
    args = ap.parse_args()

    seg_path = PROC / f"segments_{args.season}.parquet"
    if not seg_path.exists():
        print(f"no segments at {seg_path} -- run scripts/fetch_pbp.py --season "
              f"{args.season} first")
        return 1
    seg = pd.read_parquet(seg_path)
    ngames = seg["game_id"].nunique()
    print("=" * 70)
    print(f"RAPM VALIDATION  season {args.season}-{str(args.season + 1)[-2:]}  "
          f"({ngames} games, {len(seg)} segments, alpha={args.alpha:g})")
    print("=" * 70)

    rapm = fit_rapm(seg, alpha=args.alpha)
    rated = rapm[rapm["poss"] >= MIN_POSS].copy()

    # Names + box-score defensive impact for comparison.
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    box = imp[imp["season_start"] == args.season][
        ["player_id", "player_name", "def_impact", "off_impact", "impact", "minutes"]]
    m = rated.merge(box, on="player_id", how="left")

    print("\n--- CHECK 1: face validity (top defensive RAPM) ---")
    print("Positive = good defense (prevents points). Known anchors should top this.\n")
    top = m.nlargest(12, "def_rapm")
    print(top[["player_name", "def_rapm", "off_rapm", "poss"]].to_string(
        index=False, float_format=lambda v: f"{v:6.2f}"))

    print("\n--- CHECK 2: divergence from the box-score defensive metric ---")
    ok = m.dropna(subset=["def_impact", "def_rapm"])
    if len(ok) > 20:
        r = ok["def_rapm"].corr(ok["def_impact"])
        print(f"  corr(RAPM defense, box-score def_impact) = {r:.3f}  (n={len(ok)})")
        print("  Low-to-moderate is the informative outcome: RAPM sees defense the box")
        print("  score misses. corr(RAPM def, box def-rebound proxy) would be lower still.")
        # Who does RAPM rate as a good defender that the box score does not?
        ok = ok.copy()
        ok["rapm_z"] = (ok["def_rapm"] - ok["def_rapm"].mean()) / ok["def_rapm"].std()
        ok["box_z"] = (ok["def_impact"] - ok["def_impact"].mean()) / ok["def_impact"].std()
        ok["gap"] = ok["rapm_z"] - ok["box_z"]
        print("\n  RAPM rates far ABOVE the box score (defense box scores can't see):")
        print(ok.nlargest(5, "gap")[["player_name", "def_rapm", "def_impact"]].to_string(
            index=False, float_format=lambda v: f"{v:6.2f}"))
        print("  RAPM rates far BELOW the box score (empty defensive stats):")
        print(ok.nsmallest(5, "gap")[["player_name", "def_rapm", "def_impact"]].to_string(
            index=False, float_format=lambda v: f"{v:6.2f}"))

    print("\n--- CHECK 3: team defense reconstruction (in-sample) ---")
    poss_share = seg_player_possessions(seg)
    team_def = team_from_players(seg, rapm, "def_rapm", poss_share)
    team_box = team_from_players(seg, box.rename(columns={"def_impact": "def_rapm"}),
                                 "def_rapm", poss_share)
    ts = load_team_seasons()
    act = ts[ts["season_start"] == args.season][["team_id", "def_rating"]].copy()
    act["def_dev"] = -(act["def_rating"] - act["def_rating"].mean())  # + = good defense

    for label, tf in (("RAPM defense", team_def), ("box-score defense", team_box)):
        j = tf.merge(act, on="team_id", how="inner").dropna()
        if len(j) >= 10:
            r = j["team_val"].corr(j["def_dev"])
            print(f"  {label:18} vs actual team defense: r = {r:.3f}  "
                  f"(r-squared {r**2:.3f}, n={len(j)})")
    print("\n  (One-season in-sample, so partly circular; the clean test is whether this")
    print("   season's RAPM predicts NEXT season's team defense better -- needs 2 seasons.)")
    return 0


def seg_player_possessions(seg: pd.DataFrame) -> pd.DataFrame:
    """Possessions each player was on the floor for, per team."""
    hp = [f"home_p{i}" for i in range(1, 6)]
    ap = [f"away_p{i}" for i in range(1, 6)]
    rows = []
    for _, s in seg.iterrows():
        for c in hp:
            rows.append((s[c], s["home_id"], s["poss"]))
        for c in ap:
            rows.append((s[c], s["away_id"], s["poss"]))
    df = pd.DataFrame(rows, columns=["player_id", "team_id", "poss"])
    return df.groupby(["player_id", "team_id"], as_index=False)["poss"].sum()


def team_from_players(seg: pd.DataFrame, player_vals: pd.DataFrame, col: str,
                      poss_share: pd.DataFrame) -> pd.DataFrame:
    """Possession-weighted team aggregate of a per-player value."""
    d = poss_share.merge(player_vals[["player_id", col]], on="player_id", how="left")
    d = d.dropna(subset=[col])
    d["w"] = d["poss"]
    g = d.groupby("team_id").apply(
        lambda x: np.average(x[col], weights=x["w"]), include_groups=False)
    return g.rename("team_val").reset_index()


if __name__ == "__main__":
    raise SystemExit(main())
