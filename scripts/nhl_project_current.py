"""NHL Stage 6: the LIVE upcoming-season projection (points distribution per team).

Runs the shipped Stage 5 pipeline (projected skaters -> team goal rates -> season simulation ->
one-year carryover) on the **current** roster from the NHL web API, to project the upcoming season's
standings points -- mean + a calibrated 80% interval -- for every team. This is the NHL analog of
the NBA project's `scripts/project_current.py`; its output is what the Stage 6 market comparison and
the NHL "Records" page consume.

    python scripts/nhl_project_current.py                 # project 2026-27, print + write the bundle
    python scripts/nhl_project_current.py --refresh        # re-pull rosters (pick up later moves)

A projection is only as current as its roster snapshot -- trades continue all season -- so the pull
date is recorded in the bundle. Legend: points are 82-game standings points; the 80% interval is the
sim's season luck + a walk-forward projection-error term (nominal-80% coverage 0.82 in backtest).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import norm  # noqa: E402

from nhl import aggregate, gamesim, projection, rosters, season  # noqa: E402
from nhl.ingest import PROC, season_str  # noqa: E402

Z80 = norm.ppf(0.9)
OUT = PROC / "projection_current.json"
_REF = pd.read_parquet(PROC / "team_reference.parquet")
NAMES = dict(zip(_REF["tricode"], _REF["team_name"]))
TOP_N = 6  # roster-detail rows shown per team (the per-team disagreement deliverable)


def _skater_names(as_of: int) -> pd.Series:
    """player_id -> name, from the most recent MoneyPuck skater rows at/before ``as_of``."""
    sk = pd.read_parquet(PROC / "moneypuck_skaters.parquet")
    sk = sk[(sk["situation"] == "all") & (sk["season_start"] <= as_of)]
    sk = sk.sort_values("season_start").drop_duplicates("playerId", keep="last")
    sk["playerId"] = sk["playerId"].astype(int)
    return sk.set_index("playerId")["name"]


def roster_detail(toi: pd.DataFrame, proj_full: pd.DataFrame, live: pd.DataFrame,
                  names: pd.Series, *, top_n: int = TOP_N) -> dict:
    """Per-team top roster contributors -- the "who" behind each team's projection.

    Ranks every roster skater by his **contribution to the team's net rating**
    (``net_impact * (his TOI share of the team's total)``), which sums exactly to the team's
    aggregate net by construction (mirrors the NBA project's per-player win-value decomposition).
    Uncovered players (rookies / no prior season) fall to the aggregation's replacement level, same
    as the team rating itself -- so a thin rookie correctly shows near the bottom, not blank.
    """
    d = toi.merge(live[["player_id", "pos"]].drop_duplicates("player_id"), on="player_id", how="left")
    d["off"] = d["player_id"].map(proj_full["off"]).fillna(aggregate.REPLACEMENT_OFF)
    d["def"] = d["player_id"].map(proj_full["def"]).fillna(aggregate.REPLACEMENT_DEF)
    d["net"] = d["off"] + d["def"]
    d["name"] = d["player_id"].map(names).fillna("Player " + d["player_id"].astype(str))
    d["mpg"] = d["icetime"] / 60.0 / 82.0  # prior-season 5v5 min/game (a normal 82-game season)
    d["team_ice"] = d.groupby("team")["icetime"].transform("sum")
    d["contrib"] = d["net"] * (d["icetime"] / d["team_ice"])

    out: dict[str, list[dict]] = {}
    for team, grp in d.groupby("team"):
        top = grp.reindex(grp["contrib"].abs().sort_values(ascending=False).index).head(top_n)
        out[team] = [
            {"name": r["name"], "pos": r["pos"] or "?", "off": round(float(r["off"]), 3),
             "def": round(float(r["def"]), 3), "net": round(float(r["net"]), 3),
             "mpg": round(float(r["mpg"]), 1), "contrib": round(float(r["contrib"]), 4)}
            for _, r in top.iterrows()
        ]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=2026, help="season start year to project (2026 = 2026-27)")
    ap.add_argument("--refresh", action="store_true", help="re-pull live rosters from the NHL API")
    args = ap.parse_args()
    T = args.target

    # 1) backtest panel through T-1: calibration slopes, carryover rho, prior-season residuals, sigma
    years = [y for y in season.projectable_seasons(last=T - 1)]
    P = season.build_panel(years)
    P = season.walkforward_means(P)
    P = season.add_carry_residual(P)               # adds mu_resid, mu_carry, muc_resid
    cal = season.calibrate(P, season.league_gf(T - 1))   # full-panel slopes; level = last season's

    # 2) live team ratings: projected skaters onto the CURRENT roster + prior-season minutes
    proj_skaters = projection.project(T - 1)
    toi = rosters.live_toi(T, refresh=args.refresh)
    g = aggregate.team_ratings(proj_skaters, T, toi=toi).copy()
    gf, ga = season.goal_rates(g["off"].values, g["def"].values, cal)
    g["gf"], g["ga"] = gf, ga
    g["mu"] = gamesim.expected_points(gf, ga, league_gf=cal["level"])
    wins0 = gamesim.expected_wins(gf, ga, league_gf=cal["level"])   # pre-carryover, from the sim

    # 3) one-year carryover: rho on residual pairs before T, times each team's season T-1 residual
    pr = P[P["Y"] < T].merge(
        P.assign(Y=P["Y"] + 1)[["team", "Y", "mu_resid"]].rename(columns={"mu_resid": "rp"}),
        on=["team", "Y"]).dropna(subset=["mu_resid", "rp"])
    rho = float(np.polyfit(pr["rp"], pr["mu_resid"], 1)[0]) if len(pr) > 10 else 0.0
    prev_resid = P[P["Y"] == T - 1].set_index("team")["mu_resid"]
    g["carry"] = rho * g["team"].map(prev_resid).fillna(0.0)
    g["proj"] = g["mu"] + g["carry"]
    # The carryover is fit on POINTS residuals; split it onto wins at "2 points per marginal win" (the
    # standings conversion for a win vs a loss) so proj/wins stay consistent -- proj - 2*wins recovers
    # exactly the sim's own OT-loss games (>= 0 by construction), instead of the pre-carryover wins
    # figure implying a structurally-impossible negative OT-loss count against the carryover-adjusted
    # points. Standings-ready: "wins" is now the carryover-adjusted number a market comparison wants.
    g["wins"] = wins0 + g["carry"] / 2.0

    # 4) interval: sim season-luck + walk-forward projection error (from the sim+carry residual)
    sigma = season.projection_sigma(P, T)
    rng = np.random.default_rng(20)
    samp = gamesim.simulate_points(gf, ga, league_gf=cal["level"], n_sims=20000,
                                   sigma_extra=sigma, rng=rng)
    samp = samp + g["carry"].values[None, :]
    g["p10"] = np.quantile(samp, 0.10, axis=0)
    g["p50"] = np.quantile(samp, 0.50, axis=0)
    g["p90"] = np.quantile(samp, 0.90, axis=0)

    g = g.sort_values("proj", ascending=False).reset_index(drop=True)
    snap = dt.date.today().isoformat()

    # 5) per-team roster detail: which players are actually driving each projection (the "here is the
    # structural reason why" deliverable) -- HTTP-cached, so this second roster call is nearly free.
    live = rosters.live_roster(T)
    top_by_team = roster_detail(toi, proj_skaters.set_index("player_id"), live, _skater_names(T - 1))

    print(f"NHL {season_str(T)} projected standings points  (roster snapshot {snap})")
    print(f"calibration: off->GF slope {cal['a1']:+.1f}, def->GA slope {cal['b1']:+.1f}, "
          f"level {cal['level']:.2f} GF/g | carryover rho {rho:.2f} | interval SD {sigma:.1f}\n")
    print(f"{'#':>2} {'team':>5} {'proj':>6} {'80% interval':>16} {'off':>6} {'def':>6} {'carry':>6}")
    for i, r in g.iterrows():
        print(f"{i + 1:>2} {r['team']:>5} {r['proj']:>6.1f}   [{r['p10']:>5.1f}, {r['p90']:>5.1f}]   "
              f"{r['off']:>+6.3f} {r['def']:>+6.3f} {r['carry']:>+6.1f}")
    print(f"\nsum of projected points = {g['proj'].sum():.0f}  (32 teams x 82 games x ~2.28 pts/game)")

    bundle = {
        "meta": {
            "target_season": season_str(T), "target_start": T, "snapshot_date": snap,
            "model": "stage5-sim+carry", "rho": rho, "sigma": sigma,
            "level_gf": cal["level"], "a1": cal["a1"], "b1": cal["b1"],
            "note": "82-game standings points; 80% interval = season luck + projection error "
                    "(walk-forward coverage 0.82). Market lines are downstream-only, never an input.",
        },
        "teams": [
            {"team": r["team"], "name": NAMES.get(r["team"], r["team"]),
             "proj": round(float(r["proj"]), 1), "wins": round(float(r["wins"]), 1),
             "p10": round(float(r["p10"]), 1), "p50": round(float(r["p50"]), 1),
             "p90": round(float(r["p90"]), 1), "off": round(float(r["off"]), 4),
             "def": round(float(r["def"]), 4), "net": round(float(r["net"]), 4),
             "carry": round(float(r["carry"]), 1), "cover": round(float(r["cover"]), 3),
             "top": top_by_team.get(r["team"], [])}
            for _, r in g.iterrows()
        ],
    }
    OUT.write_text(json.dumps(bundle, indent=2))
    print(f"\nwrote {OUT.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
