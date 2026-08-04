"""NHL Stage 2: player-impact face-validity report.

Runs the xG-RAPM skater estimator and the goalie GSAx module for a season and prints the
leaders, as a sanity check that the impact metric is measuring the right thing before it is
wired into a team projection (Stage 4). This is validation, not a gate.

    python scripts/nhl_impact_report.py --season 2023 --alpha 1500

Requires the season's shift charts (scripts/nhl_fetch_shifts.py) and MoneyPuck shots.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nhl import goalies, rapm  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402

LEGEND = """\
Legend (acronyms expanded on first use):
  xG-RAPM  regularized adjusted plus-minus on expected goals -- a ridge regression that
           credits a skater's on-ice impact while controlling for his 4 teammates and 5
           opponents. off/def/net are expected-goals-per-60-minutes rate impacts.
  off      xG per 60 a skater ADDS on offense (higher = better)
  def      xG per 60 a skater SUPPRESSES on defense (higher = better; = -[xG allowed])
  net      off + def
  GSAx     goals saved above expected = expected goals against - actual goals against
  TOI      time on ice (here, even-strength 5v5 minutes)
"""


def _names(season_start: int) -> dict[int, str]:
    """player_id -> name, from the MoneyPuck skater summary (covers everyone who played)."""
    sk = pd.read_parquet(PROC / "moneypuck_skaters.parquet")
    sk = sk[sk["season_start"] == season_start][["playerId", "name"]].drop_duplicates()
    return dict(zip(sk["playerId"].astype(int), sk["name"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2023)
    ap.add_argument("--alpha", type=float, default=rapm.DEFAULT_ALPHA)
    args = ap.parse_args()

    if not (PROC / f"shifts_{args.season}.parquet").exists():
        print(f"missing data/nhl/processed/shifts_{args.season}.parquet -- run the shift pull first")
        return 1

    print(LEGEND)
    print(f"=== {season_str(args.season)} — xG-RAPM (alpha={args.alpha:g}) ===")
    sk = rapm.season_rapm(args.season, alpha=args.alpha)
    names = _names(args.season)
    sk["name"] = sk["player_id"].map(names).fillna(sk["player_id"].astype(str))

    show = ["name", "toi_min", "off", "def", "net"]
    fmt = lambda x: f"{x:6.2f}"  # noqa: E731
    print(f"\n-- top 12 by NET (5v5, TOI floor {rapm.DEFAULT_MIN_TOI // 60} min) --")
    print(sk.head(12)[show].to_string(index=False, float_format=fmt))
    print("\n-- top 8 by OFFENSE --")
    print(sk.sort_values("off", ascending=False).head(8)[show].to_string(index=False, float_format=fmt))
    print("\n-- top 8 by DEFENSE --")
    print(sk.sort_values("def", ascending=False).head(8)[show].to_string(index=False, float_format=fmt))

    print(f"\n=== {season_str(args.season)} — goalie GSAx (>=1500 min) ===")
    g = goalies.goalie_gsax("all")
    g = g[(g["season_start"] == args.season) & (g["toi_min"] >= 1500)]
    print(g.head(10)[["name", "games", "xga", "ga", "gsax", "gsax_per_60"]].to_string(
        index=False, float_format=fmt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
