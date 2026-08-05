"""NHL Stage 5: the goal-based season simulation, gated against the shipped linear projection.

Stages 3-4 map a team's projected 5v5 strength straight to standings points with one linear fit
(``net -> pts82``), giving a point estimate but no distribution. Stage 5 instead turns strength into
projected **goals-for / goals-against per game** (offense and defense calibrated separately,
drift-tracked to the prior season's league scoring level), then **simulates the games**
(``nhl.gamesim``): Poisson goals -> regulation/OT/shootout branch -> 2/1/0 points -> summed over an
82-game schedule, many times, for a points **distribution** with a calibrated interval.

This scores the sim head-to-head with the shipped linear model on the EXACT same honest roster,
one-year carryover, and walk-forward folds, so the only thing that changes is strength->points:

    python scripts/nhl_stage5_sim_report.py                 # widest folds the data supports
    python scripts/nhl_stage5_sim_report.py --folds 2023 2024 2025

Legend (points = 82-game-equivalent standings points; lower MAE is better):
  MAE          mean absolute error (average miss, direction ignored)
  linear+carry the shipped model: linear net->pts82, + rho*(last-season residual)   [honest ~10.61]
  sim+carry    this stage: goal-based simulation mean, + the same carryover
  naive        the Stage 1 bar = mean-reverted previous points (must beat 10.54)
  cover80      share of teams whose actual points fell in the sim's nominal-80% interval (target .80)
  luckSD       season-luck SD from the sim (fixed talent); projSD = fitted projection-error term
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import norm  # noqa: E402

from nhl import aggregate, gamesim, goalies, projection, rosters  # noqa: E402
from nhl.ingest import PROC, season_id, season_str  # noqa: E402
from nhl.teams import SHORTENED_SEASONS  # noqa: E402

REF = pd.read_parquet(PROC / "team_reference.parquet")[["team_id", "tricode"]]
Z80 = norm.ppf(0.9)  # half-width multiplier for a nominal 80% interval (p10..p90)


def team_actuals(Y: int) -> pd.DataFrame:
    """Actual per-team goals-for/against per game and 82-game points for season Y."""
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)].merge(REF, left_on="teamId", right_on="team_id")
    ts["pts82"] = ts["points"] / (2 * ts["gamesPlayed"]) * 164.0
    return ts.rename(columns={"tricode": "team", "goalsForPerGame": "gf",
                              "goalsAgainstPerGame": "ga"})[["team", "gf", "ga", "pts82"]]


def league_gf(Y: int) -> float:
    """League mean goals-for per team per game in season Y (used as the next season's level est.)."""
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)]
    return float(ts["goalsForPerGame"].mean())


# Special-teams persistence (YoY corr ~0.355 on net PP/PK goal-diff, 2010-2025) -- like the goalie
# term, deliberately fixed; the point of the A/B is whether it survives the one-year carryover.
ST_PERSISTENCE = 0.36


def team_st_goaldiff(Y: int) -> pd.Series:
    """Projected special-teams net goal-differential per game for season ``Y+1``, from team ``Y``'s
    power-play + penalty-kill NET percentages (each ~3 situations/game), regressed toward 0."""
    ts = pd.read_parquet(PROC / "team_summary.parquet")
    ts = ts[ts["seasonId"] == season_id(Y)].merge(REF, left_on="teamId", right_on="team_id")
    st = 3.0 * (ts["powerPlayNetPct"] + ts["penaltyKillNetPct"] - 1.0)
    return pd.Series(ST_PERSISTENCE * st.values, index=ts["tricode"].values)


def build_panel(years: list[int]) -> pd.DataFrame:
    """Projected 5v5 off/def/net (honest roster) + actual gf/ga/pts82 per team-season."""
    rows = []
    for Y in years:
        g = aggregate.team_ratings(projection.project(Y - 1), Y, toi=rosters.honest_toi(Y))
        rows.append(g[["team", "off", "def", "net"]].merge(team_actuals(Y), on="team").assign(Y=Y))
    return pd.concat(rows, ignore_index=True)


def _walkforward_means(P: pd.DataFrame) -> pd.DataFrame:
    """Fill each panel season's LINEAR and SIM projected-points means, each calibrated on strictly
    earlier seasons (point-in-time). Sim: off->gf, def->ga (slope from training, level re-anchored to
    the prior season's league goals so scoring drift is tracked), then gamesim.expected_points."""
    P = P.copy()
    P["lin"] = np.nan       # shipped linear net -> pts82
    P["mu"] = np.nan        # goal-based sim (skaters only)
    P["mug"] = np.nan       # goal-based sim + projected goaltending in the GA rate
    P["mus"] = np.nan       # goal-based sim + projected special teams in the goal differential
    P["luckvar"] = np.nan
    for s in sorted(P["Y"].unique()):
        tr = P[P["Y"] < s]
        if len(tr) < 10:
            continue
        te = P["Y"] == s
        sub = P[te]
        c1, c0 = np.polyfit(tr["net"], tr["pts82"], 1)
        P.loc[te, "lin"] = c0 + c1 * sub["net"]
        # goal-based: slopes on training, level from prior-season league scoring (drift, no leak)
        a1 = np.polyfit(tr["off"], tr["gf"], 1)[0]
        b1 = np.polyfit(tr["def"], tr["ga"], 1)[0]
        Lp = league_gf(s - 1)
        gf_pred = (Lp + a1 * (sub["off"] - sub["off"].mean())).values
        ga_pred = (Lp + b1 * (sub["def"] - sub["def"].mean())).values
        P.loc[te, "mu"] = gamesim.expected_points(gf_pred, ga_pred, league_gf=Lp)
        # goaltending: subtract each team's projected goals-saved-per-game from its GA rate
        tg = goalies.team_gsax_per_game(s - 1).set_index("team")["gsax_pg"]
        gadj = sub["team"].map(tg).fillna(0.0).values
        P.loc[te, "mug"] = gamesim.expected_points(gf_pred, ga_pred - gadj, league_gf=Lp)
        # special teams: shift the goal differential by the projected PP/PK net (half onto GF, half GA)
        st = sub["team"].map(team_st_goaldiff(s - 1)).fillna(0.0).values
        P.loc[te, "mus"] = gamesim.expected_points(gf_pred + st / 2, ga_pred - st / 2, league_gf=Lp)
        # season-luck variance from the sim (fixed talent) for the interval decomposition
        samp = gamesim.simulate_points(gf_pred, ga_pred, league_gf=Lp,
                                       n_sims=4000, rng=np.random.default_rng(11))
        P.loc[te, "luckvar"] = samp.var(axis=0)
    return P


def _carryover(P: pd.DataFrame, Y: int, col: str) -> pd.Series:
    """rho*(last-season residual) for the projection in `col`, rho fit on residual pairs before Y."""
    resid = P[col + "_resid"]
    pr = P[P["Y"] < Y].merge(
        P.assign(Y=P["Y"] + 1)[["team", "Y", col + "_resid"]].rename(columns={col + "_resid": "rp"}),
        on=["team", "Y"]).dropna(subset=[col + "_resid", "rp"])
    rho = float(np.polyfit(pr["rp"], pr[col + "_resid"], 1)[0]) if len(pr) > 10 else 0.0
    prev = P[P["Y"] == Y - 1][["team", col + "_resid"]].rename(columns={col + "_resid": "prev"})
    te = P[P["Y"] == Y][["team"]].merge(prev, on="team", how="left").fillna({"prev": 0.0})
    return rho, te.set_index("team")["prev"] * rho


def naive_bar(P: pd.DataFrame, Y: int, gmean: float) -> float:
    """Stage 1 bar for fold Y: mean-reverted previous points, reversion k fit on prior transitions."""
    te = team_actuals(Y).rename(columns={"pts82": "act"})
    prk = pd.concat([team_actuals(y - 1).rename(columns={"pts82": "prev"})[["team", "prev"]]
                     .assign(Y=y) for y in range(int(P["Y"].min()) + 1, Y)])
    trk = P[P["Y"] < Y][["team", "Y", "pts82"]].merge(prk, on=["team", "Y"])
    k = float(np.polyfit(trk["prev"] - gmean, trk["pts82"] - gmean, 1)[0]) if len(trk) > 10 else 0.5
    prev = team_actuals(Y - 1).rename(columns={"pts82": "prev"})[["team", "prev"]]
    m = te.merge(prev, on="team")
    return float((gmean + k * (m["prev"] - gmean) - m["act"]).abs().mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", type=int, default=None)
    ap.add_argument("--last", type=int, default=2025)
    ap.add_argument("--folds", type=int, nargs="+", default=None)
    ap.add_argument("--detail", type=int, default=None,
                    help="print the full per-team points distribution (mean, 80%% interval, actual) "
                         "for one fold season, to eyeball the simulation output")
    args = ap.parse_args()

    imp = {int(Path(f).stem.split("_")[1])
           for f in glob.glob(str(PROC / f"impact_*_a{int(projection.rapm.DEFAULT_ALPHA)}.parquet"))}
    have_shifts = {int(Path(f).stem.split("_")[1]) for f in glob.glob(str(PROC / "shifts_*.parquet"))}
    years = [Y for Y in range((args.first or 2011), args.last + 1)
             if any(s in imp for s in range(Y - 3, Y)) and Y in have_shifts]
    folds = args.folds or [Y for Y in years if Y >= years[0] + 4]

    print(f"Panel seasons: {season_str(years[0])}..{season_str(years[-1])} ({len(years)}); "
          f"scoring folds {', '.join(season_str(f) for f in folds)}\n")

    P = build_panel(years)
    P = _walkforward_means(P)
    for c in ["lin", "mu", "mug", "mus"]:
        P[c + "_resid"] = P["pts82"] - P[c]
    # walk-forward sim+carry prediction for every panel season, so the interval width is the SD of
    # the actual (post-carryover) residual -- not the wider plain-sim residual it is centered away from.
    P["mu_carry"] = np.nan
    for s in sorted(P["Y"].unique()):
        sub = P[P["Y"] == s]
        if sub["mu"].isna().all():
            continue
        _, carry = _carryover(P, s, "mu")
        P.loc[P["Y"] == s, "mu_carry"] = sub["mu"].values + sub["team"].map(carry).fillna(0.0).values
    P["muc_resid"] = P["pts82"] - P["mu_carry"]
    gmean = P["pts82"].mean()

    print("Goal-based SIMULATION vs the shipped LINEAR projection (same honest roster + carryover).")
    print("simG = sim + projected goaltending;  simS = sim + projected special teams.")
    print("(* = lockout/covid-shortened season -- pt%-normalized, excluded from the headline)\n")
    print(f"{'season':>9} {'lin+car':>8} {'sim+car':>8} {'simG+car':>9} {'simS+car':>9} {'naive':>7} "
          f"{'cover80':>8} {'luckSD':>7} {'projSD':>7} {'rho':>5} {'n':>4}")
    rows = []
    for Y in folds:
        te = P[P["Y"] == Y].dropna(subset=["mu", "lin"]).copy()
        if te.empty:
            continue
        _, carry_l = _carryover(P, Y, "lin")
        rho_s, carry_s = _carryover(P, Y, "mu")
        _, carry_g = _carryover(P, Y, "mug")
        _, carry_st = _carryover(P, Y, "mus")
        te = te.set_index("team")
        pred_lin = te["lin"] + carry_l
        pred_sim = te["mu"] + carry_s
        pred_simg = te["mug"] + carry_g
        pred_sims = te["mus"] + carry_st

        # interval: total predictive SD = pooled sim+carry residual SD on PRIOR folds; decompose into
        # season luck (from the sim) + projection error (the remainder).
        prior = P[P["Y"] < Y].dropna(subset=["muc_resid"])
        sig_total = float(prior["muc_resid"].std()) if len(prior) > 10 else float(te["muc_resid"].std())
        luck_sd = float(np.sqrt(te["luckvar"].mean()))
        proj_sd = float(np.sqrt(max(sig_total ** 2 - luck_sd ** 2, 0.0)))
        half = Z80 * sig_total
        act = te["pts82"]
        cover = float(((act >= pred_sim - half) & (act <= pred_sim + half)).mean())

        mae_lin = float((pred_lin - act).abs().mean())
        mae_sim = float((pred_sim - act).abs().mean())
        mae_simg = float((pred_simg - act).abs().mean())
        mae_sims = float((pred_sims - act).abs().mean())
        mae_nv = naive_bar(P, Y, gmean)

        if args.detail == Y:
            lo, hi = pred_sim - half, pred_sim + half
            det = pd.DataFrame({"proj": pred_sim, "lo": lo, "hi": hi, "actual": act})
            det = det.sort_values("proj", ascending=False)
            print(f"\n--- {season_str(Y)} projected standings points (sim+carry, nominal-80% interval) ---")
            print(f"{'team':>5} {'proj':>6} {'80% interval':>16} {'actual':>7}  hit")
            for tm, r in det.iterrows():
                inside = "in " if r["lo"] <= r["actual"] <= r["hi"] else "OUT"
                print(f"{tm:>5} {r['proj']:>6.1f}   [{r['lo']:>5.1f}, {r['hi']:>5.1f}]   "
                      f"{r['actual']:>6.1f}  {inside}")
            print()
        star = "*" if Y in SHORTENED_SEASONS else " "
        print(f"{season_str(Y)+star:>9} {mae_lin:>8.2f} {mae_sim:>8.2f} {mae_simg:>9.2f} {mae_sims:>9.2f} "
              f"{mae_nv:>7.2f} {cover:>8.2f} {luck_sd:>7.1f} {proj_sd:>7.1f} {rho_s:>5.2f} {len(te):>4}")
        rows.append({"Y": Y, "lin": mae_lin, "sim": mae_sim, "simg": mae_simg, "sims": mae_sims,
                     "naive": mae_nv, "cover": cover, "n": len(te)})

    R = pd.DataFrame(rows)
    full = R[~R["Y"].isin(SHORTENED_SEASONS)]
    n_short = len(R) - len(full)
    print()
    print(f"{'mean·all':>9} {R['lin'].mean():>8.2f} {R['sim'].mean():>8.2f} {R['simg'].mean():>9.2f} "
          f"{R['sims'].mean():>9.2f} {R['naive'].mean():>7.2f} {R['cover'].mean():>8.2f}   (all {len(R)} folds)")
    if n_short:
        print(f"{'mean·full':>9} {full['lin'].mean():>8.2f} {full['sim'].mean():>8.2f} "
              f"{full['simg'].mean():>9.2f} {full['sims'].mean():>9.2f} {full['naive'].mean():>7.2f} "
              f"{full['cover'].mean():>8.2f}   (headline; excl {n_short} shortened; Stage 1 bar 10.54)")
    h = full
    print(f"\nheadline over {len(h)} full-season folds:")
    print(f"  sim+carry            = {h['sim'].mean():.2f} points  (vs linear+carry {h['lin'].mean():.2f}: "
          f"{h['sim'].mean() - h['lin'].mean():+.2f})")
    print(f"  + goalie lever       = {h['simg'].mean():.2f} points  ({h['simg'].mean() - h['sim'].mean():+.2f} "
          f"vs sim+carry)")
    print(f"  + special-tms lever  = {h['sims'].mean():.2f} points  ({h['sims'].mean() - h['sim'].mean():+.2f} "
          f"vs sim+carry)")
    print(f"\n  shipped sim+carry vs walk-forward naive bar: {h['sim'].mean() - h['naive'].mean():+.2f}")
    print(f"  shipped sim+carry vs Stage 1 bar (10.54):    {h['sim'].mean() - 10.54:+.2f}")
    print(f"  nominal-80% interval coverage: {h['cover'].mean():.2f}  (target 0.80)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
