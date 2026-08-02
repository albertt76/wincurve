"""Surface candidates for the injury-return override (data/overrides/injury_returns.json).

Finds rostered players for the upcoming season who missed a large chunk of LAST season but
were productive in a recent healthy one -- the profile of a star coming back from a
season-long injury, who the model otherwise mis-projects (his minutes fall back to a bench
default and the availability model marks him down).

This scan is OBJECTIVE and data-only: it cannot know WHY a player missed time (injury vs
trade vs rest vs decline) or his medical prognosis for next season. Those are manual
judgment calls -- decide them per player and record them in injury_returns.json. Treat this
as a shortlist to review, not a list to import wholesale. Players already in the override
file are flagged so you can see what is covered.

Legend (all impact figures are our box+RAPM metric, in points per 100 possessions relative
to an average player; positive is good):
  healthy_impact  overall impact in the player's last healthy season (the basis)
  healthy_mpg     minutes per game that season -- the role the override would restore
  g_last          games he actually played LAST season (the injury year)
  est_lost        healthy_impact x healthy_mpg x (share of last season missed) -- a rough
                  "how much productive player-time went missing", for ranking only
  in_list         whether he is already in injury_returns.json

    python scripts/injury_return_candidates.py
    python scripts/injury_return_candidates.py --max-games 45 --min-healthy-minutes 1200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from nbaproj.rosters import load_return_overrides  # noqa: E402

PROC = Path("data/processed")
TARGET = 2026        # upcoming season (season_start)
LAST_HISTORY = 2025  # last completed season (the potential injury year)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-games", type=int, default=45,
                    help="a player is a candidate if he played <= this many games last "
                         "season (default 45, i.e. missed more than half)")
    ap.add_argument("--min-healthy-minutes", type=int, default=1200,
                    help="minutes that make a prior season count as 'healthy' (default 1200)")
    ap.add_argument("--all", action="store_true",
                    help="show every candidate, including negative-impact healthy seasons")
    args = ap.parse_args()

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    cur = pd.read_parquet(PROC / "rosters_current.parquet").rename(
        columns={"TeamID": "team_id", "PLAYER_ID": "player_id"})
    cur_ids = set(cur["player_id"].astype("int64"))
    team_of = cur.drop_duplicates("player_id").set_index(
        cur.drop_duplicates("player_id")["player_id"].astype("int64"))["team_id"]

    from nba_api.stats.static import teams as static_teams
    abbr = {t["id"]: t["abbreviation"] for t in static_teams.get_teams()}

    already = set(load_return_overrides()["player_id"].astype("int64"))

    # A "healthy basis" season: the most recent of the two completed seasons before last
    # (so it predates the injury year) with at least --min-healthy-minutes minutes.
    basis_window = [LAST_HISTORY - 2, LAST_HISTORY - 1]

    rows = []
    for pid, g in imp[imp["player_id"].isin(cur_ids)].groupby("player_id"):
        g = g.sort_values("season_start")
        last = g[g["season_start"] == LAST_HISTORY]
        g_last = int(last["games"].iloc[0]) if len(last) else 0
        if g_last > args.max_games:
            continue
        healthy = g[(g["season_start"].isin(basis_window))
                    & (g["minutes"] >= args.min_healthy_minutes)]
        if healthy.empty:
            continue
        hb = healthy.iloc[-1]
        impact_h = float(hb["impact"])
        if impact_h <= 0 and not args.all:
            continue
        mpg_h = float(hb["minutes"]) / max(float(hb["games"]), 1.0)
        basis = int(hb["season_start"])
        age_next = int(hb["age"]) + (TARGET - basis) if pd.notna(hb["age"]) else None
        rows.append({
            "name": str(hb["player_name"]),
            "team": abbr.get(int(team_of.get(int(pid), -1)), "?"),
            "age": age_next,
            "basis": f"{basis}-{str(basis + 1)[-2:]}",
            "healthy_impact": round(impact_h, 2),
            "healthy_mpg": round(mpg_h, 1),
            "g_last": g_last,
            "est_lost": round(impact_h * mpg_h * (1 - g_last / 82.0), 1),
            "in_list": "yes" if int(pid) in already else "",
        })

    df = pd.DataFrame(rows).sort_values("est_lost", ascending=False)
    pd.set_option("display.width", 200)
    print(__doc__.split("\n\n")[0])  # one-line summary
    print(f"\nUpcoming: {TARGET}-{str(TARGET + 1)[-2:]}   injury year scanned: "
          f"{LAST_HISTORY}-{str(LAST_HISTORY + 1)[-2:]}   "
          f"(<= {args.max_games} games last season, healthy basis >= "
          f"{args.min_healthy_minutes} min)\n")
    print(df.to_string(index=False))
    n_new = (df["in_list"] == "").sum()
    print(f"\n{len(df)} candidates, {len(df) - n_new} already in injury_returns.json, "
          f"{n_new} not yet reviewed.")
    print("Reminder: this is a data scan, not a medical judgment. Whether each player is a "
          "genuine\nreturn at his prior level -- vs chronic absence, trade, rest, or age "
          "decline -- is a manual\ncall to record in data/overrides/injury_returns.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
