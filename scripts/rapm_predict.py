"""The cross-season predictive test: does RAPM defense predict NEXT season's team defense
better than the box-score defensive metric?

This is the non-circular test that decides whether RAPM is worth integrating. For each
target season T, a team's defense is predicted from its T-roster's players valued by their
PRIOR-season (T-1) defensive metric, possession-weighted by their role in T. Whichever
prior metric better predicts the actual season-T team defensive rating is the better metric.
Because the player values come only from before season T, this is genuinely out-of-sample.

    python scripts/rapm_predict.py --alpha 2000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.bulk_pbp import segments_for_season  # noqa: E402
from nbaproj.rapm import fit_rapm  # noqa: E402
from nbaproj.teams import load_team_seasons  # noqa: E402

PROC = Path("data/processed")
SEASONS = [2021, 2022, 2023, 2024]  # 2021-22 .. 2024-25 (bulk-available)


def season_rapm(season: int, alpha: float, prior: pd.DataFrame) -> pd.DataFrame:
    """Box-informed RAPM for one season, cached to parquet."""
    out = PROC / f"rapm_{season}_a{int(alpha)}.parquet"
    if out.exists():
        return pd.read_parquet(out)
    seg = segments_for_season(season)
    r = fit_rapm(seg, alpha=alpha, prior=prior)
    r.to_parquet(out, index=False)
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=2000.0)
    args = ap.parse_args()

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    ts = load_team_seasons()
    ts = ts.copy()
    # Actual team defense as a deviation, positive = good (fewer points allowed).
    ts["def_dev"] = -(ts["def_rating"]
                      - ts.groupby("season_start")["def_rating"].transform("mean"))

    # Per-season box-informed RAPM, each anchored on that season's box prior.
    rapm = {}
    for s in SEASONS:
        prior = imp[imp["season_start"] == s][
            ["player_id", "off_impact", "def_impact"]].rename(
            columns={"off_impact": "off_prior", "def_impact": "def_prior"})
        rapm[s] = season_rapm(s, args.alpha, prior)

    print("=" * 72)
    print("CROSS-SEASON PREDICTIVE TEST")
    print("=" * 72)
    print("Predict each team's season-T defense from its T-roster valued by PRIOR-season")
    print("(T-1) defense. Correlation with actual season-T team defense; higher is better.")
    print("Out-of-sample: player values come only from before season T.\n")
    print(f"{'target':>8} {'n_teams':>8} {'RAPM r':>8} {'box r':>8}  winner")
    print("-" * 50)

    rows = []
    for target in SEASONS[1:]:
        prev = target - 1
        # Season-T roster: possessions each player logged for each team in T.
        roster = pts[pts["season_start"] == target].groupby(
            ["team_id", "player_id"], as_index=False)["minutes"].sum()

        rapm_prev = rapm[prev][["player_id", "def_rapm"]]
        box_prev = imp[imp["season_start"] == prev][["player_id", "def_impact"]]

        team_rapm = _team_predict(roster, rapm_prev, "def_rapm")
        team_box = _team_predict(roster, box_prev, "def_impact")

        act = ts[ts["season_start"] == target][["team_id", "def_dev"]]
        jr = team_rapm.merge(act, on="team_id").dropna()
        jb = team_box.merge(act, on="team_id").dropna()
        r_rapm = jr["pred"].corr(jr["def_dev"])
        r_box = jb["pred"].corr(jb["def_dev"])
        winner = "RAPM" if r_rapm > r_box else "box"
        rows.append((r_rapm, r_box))
        print(f"  {prev}->{target}  {len(jr):>6}   {r_rapm:>7.3f} {r_box:>7.3f}  {winner}")

    rr = np.mean([r for r, _ in rows])
    rb = np.mean([b for _, b in rows])
    print("-" * 50)
    print(f"  {'MEAN':>14}   {rr:>7.3f} {rb:>7.3f}  "
          f"{'RAPM' if rr > rb else 'box'}")
    print("\nInterpretation:")
    if rr > rb + 0.02:
        print("  RAPM defense predicts next-season team defense BETTER than the box score.")
        print("  => worth integrating; run the end-to-end win-projection test next.")
    elif rb > rr + 0.02:
        print("  Box-score defense predicts as well or better here.")
        print("  => RAPM does not clear the bar on this test; investigate before shipping.")
    else:
        print("  Too close to call on 3 transitions -- pull more seasons to decide.")
    print("\n  Caveat: only 3 season-transitions (2021-22 .. 2024-25 is what the bulk mirror")
    print("  currently offers). This is indicative, not decisive; more seasons sharpen it.")
    return 0


def _team_predict(roster: pd.DataFrame, player_vals: pd.DataFrame,
                  col: str) -> pd.DataFrame:
    """Possession-weighted team aggregate of a prior-season per-player value.

    Players with no prior-season value (rookies, returnees) are dropped from the weighted
    mean -- the same players are dropped for both metrics, so the comparison stays fair.
    """
    d = roster.merge(player_vals, on="player_id", how="left").dropna(subset=[col])
    g = d.groupby("team_id").apply(
        lambda x: np.average(x[col], weights=x["minutes"]), include_groups=False)
    return g.rename("pred").reset_index()


if __name__ == "__main__":
    raise SystemExit(main())
