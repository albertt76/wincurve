"""Gate the injury-recovery offensive discount end-to-end, walk-forward.

scripts/precheck_injury_recovery.py found a real, offense-specific dip in the immediate
return season (n=155 clean cohort, delta_off -0.36 +/- 0.09 SE) and this project's own
history says ideas of exactly this shape (real at the player level, small affected
population) often wash out at the team-win level -- so this is a genuine gate, not a
formality. Restricted to team-seasons containing at least one qualifying returning player
(most team-seasons have none), since that is where the discount can possibly matter.

For each walk-forward season, a player qualifies (walk-forward safe: only uses seasons
strictly before the target) if his season target-2 was healthy (>= MIN_HEALTHY_MINUTES) and
target-1 was injury-like (games < 0.5 * that season's team-game count) -- the same detection
scripts/precheck_injury_recovery.py used, re-keyed to fire prospectively at each test season
instead of retrospectively over the whole backbone.

    python scripts/gate_injury_recovery.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.aging import aging_curves, build_transitions, project_next_season, replacement_level  # noqa: E402
from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.minutes import MINUTES_PER_GAME  # noqa: E402
from nbaproj.project import calibrate_projected_ratings, roster_opening_day  # noqa: E402
from nbaproj.rosters import MIN_HEALTHY_MINUTES, RECOVERY_OFFENSE_DISCOUNT, apply_recovery_discount  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season,
)
from nbaproj.teams import FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
N_SIMS = 5000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}


def qualifying_returns(imp: pd.DataFrame, team_games_by_season: pd.Series,
                       target_season: int) -> pd.DataFrame:
    """Walk-forward-safe detection of immediate-return players for `target_season`: healthy
    at target-2 (>= MIN_HEALTHY_MINUTES), injury-like at target-1 (games < 0.5 * team games).
    Returns a `resolved`-shaped frame (player_id, basis_season) for apply_recovery_discount."""
    hist = imp[imp["season_start"] < target_season]
    basis = hist[(hist["season_start"] == target_season - 2)
                & (hist["minutes"] >= MIN_HEALTHY_MINUTES)]
    injury = hist[hist["season_start"] == target_season - 1].copy()
    injury["team_games"] = injury["season_start"].map(team_games_by_season)
    injury = injury[injury["games"] < 0.5 * injury["team_games"]]
    qualifying_ids = set(basis["player_id"]) & set(injury["player_id"])
    return pd.DataFrame({"player_id": list(qualifying_ids),
                         "basis_season": target_season - 2})


def build_roster(imp, pts, pgl, rosters, ts, ages, season: int) -> pd.DataFrame:
    hist = imp[imp["season_start"] < season]
    rep_off = replacement_level(hist, "off_impact")
    rep_def = replacement_level(hist, "def_impact")
    curves = aging_curves(build_transitions(hist, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    talent_off = project_next_season(hist, curves, target_season=season,
                                     skill="off_impact").set_index("player_id")["proj_off_impact"]
    talent_def = project_next_season(hist, curves, target_season=season,
                                     skill="def_impact").set_index("player_id")["proj_def_impact"]

    base = roster_opening_day(rosters, pgl, season)
    if base.empty:
        return base
    prev = pts[pts["season_start"] == season - 1].groupby(
        "player_id", as_index=False).agg(m=("minutes", "sum"), g=("games", "sum"))
    prev["prior_mpg"] = prev["m"] / prev["g"].clip(lower=1)
    base = base.merge(prev[["player_id", "prior_mpg"]], on="player_id", how="left")
    base["prior_mpg"] = base["prior_mpg"].fillna(8.0)
    base["proj_off_impact"] = base["player_id"].map(talent_off).fillna(rep_off)
    base["proj_def_impact"] = base["player_id"].map(talent_def).fillna(rep_def)

    from nbaproj.availability import build_availability, build_features, fit_predict
    avail = fit_predict(build_features(build_availability(pts, ts), ages), target_season=season)
    base = base.merge(avail[["player_id", "proj_availability"]], on="player_id", how="left")
    base["proj_availability"] = base["proj_availability"].fillna(0.85)
    return base, rep_off, rep_def


def aggregate(base: pd.DataFrame, rep_off: float, rep_def: float) -> pd.DataFrame:
    budget = MINUTES_PER_GAME * FULL_SEASON_GAMES
    rows = []
    for tid, g in base.groupby("team_id"):
        mins = (g["prior_mpg"] * g["proj_availability"] * FULL_SEASON_GAMES).to_numpy()
        used = mins.sum()
        if used > budget:
            mins = mins * budget / used
            used = budget
        leftover = max(budget - used, 0.0)
        off_v = g["proj_off_impact"].to_numpy(dtype=float)
        def_v = g["proj_def_impact"].to_numpy(dtype=float)
        agg_off = (float(np.sum(mins * off_v)) + leftover * rep_off) / budget
        agg_def = (float(np.sum(mins * def_v)) + leftover * rep_def) / budget
        rows.append({"team_id": tid, "agg_off": agg_off, "agg_def": agg_def,
                     "agg_impact": agg_off + agg_def})
    return pd.DataFrame(rows)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    ages = pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()
    team_games_by_season = ts.groupby("season_start")["games"].median()

    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "net_rating_dev", "actual_wins_82", "games"]]

    def run(apply_discount: bool):
        cal, affected_teams_by_season = [], {}
        for season in range(FIRST_TEST - 1, LAST_TEST + 1):
            out = build_roster(imp, pts, pgl, rosters, ts, ages, season)
            if not isinstance(out, tuple):
                continue
            base, rep_off, rep_def = out
            resolved = qualifying_returns(imp, team_games_by_season, season)
            affected_teams_by_season[season] = set(
                base[base["player_id"].isin(resolved["player_id"])]["team_id"])
            if apply_discount and not resolved.empty:
                base = apply_recovery_discount(base, resolved, target_season=season)
            A = aggregate(base, rep_off, rep_def)
            A["season_start"] = season
            frame_so_far = pd.concat(cal + [A], ignore_index=True) if cal else A
            slope, icept = calibrate_projected_ratings(
                frame_so_far, ts, target_season=season)
            A["pred_net_rating_dev"] = slope * A["agg_impact"] + icept
            cal.append(A)
        calibrated = pd.concat(cal, ignore_index=True)

        rows = []
        for season in range(FIRST_TEST, LAST_TEST + 1):
            sub = calibrated[calibrated["season_start"] == season].copy()
            if sub.empty:
                continue
            sub = apply_carryover(sub, calibrated, ts, target_season=season)
            sched = extract_schedule(gl, season)
            hca, msd = estimate_game_params(gl, before_season=season)
            sigma = fit_rating_sigma(calibrated, ac, before_season=season)
            sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched,
                                        hca=hca, margin_sd=msd, sigma_rating=sigma,
                                        n_sims=N_SIMS, seed=2000 + season)
            j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=season)
                 .merge(ac, on=["team_id", "season_start"]))
            gp = FULL_SEASON_GAMES / j["games"].fillna(FULL_SEASON_GAMES)
            order = {t: i for i, t in enumerate(sim["team_id"])}
            w82 = wins[:, j["team_id"].map(order).to_numpy()] * gp.to_numpy()[None, :]
            actv = j["actual_wins_82"].to_numpy()
            pred = w82.mean(axis=0)
            affected = affected_teams_by_season.get(season, set())
            is_aff = j["team_id"].isin(affected).to_numpy()
            rows.append({
                "season": season, "n_affected": int(is_aff.sum()),
                "MAE_all": float(np.abs(actv - pred).mean()),
                "MAE_affected": float(np.abs(actv[is_aff] - pred[is_aff]).mean())
                if is_aff.any() else np.nan,
                "short": (season - 1) in SHORT or season in SHORT,
            })
        return pd.DataFrame(rows)

    base_df = run(False)
    disc_df = run(True)
    m = base_df.merge(disc_df, on="season", suffixes=("_base", "_disc"))
    m["delta_all"] = m["MAE_all_base"] - m["MAE_all_disc"]
    m["delta_affected"] = m["MAE_affected_base"] - m["MAE_affected_disc"]

    print("=" * 72)
    print(f"INJURY-RECOVERY OFFENSIVE DISCOUNT GATE  "
          f"(discount={RECOVERY_OFFENSE_DISCOUNT}, {N_SIMS} sims/fold)")
    print("=" * 72)
    print("MAE = mean absolute error in wins; delta > 0 means the discount helped.\n")
    show = m[["season", "n_affected_base", "MAE_all_base", "MAE_all_disc", "delta_all",
             "MAE_affected_base", "MAE_affected_disc", "delta_affected", "short_base"]]
    print(show.to_string(index=False, float_format=lambda v: f"{v:6.3f}"))

    ex = m[~m["short_base"]]
    d_all = ex["delta_all"].mean()
    se_all = ex["delta_all"].std() / np.sqrt(len(ex))
    valid_aff = ex.dropna(subset=["delta_affected"])
    d_aff = valid_aff["delta_affected"].mean() if len(valid_aff) else float("nan")
    se_aff = (valid_aff["delta_affected"].std() / np.sqrt(len(valid_aff))
             if len(valid_aff) > 1 else float("nan"))
    print(f"\n  ALL teams, excl-short: delta {d_all:+.4f} +/- {se_all:.4f} SE, "
          f"{(ex['delta_all'] > 0).sum()}/{len(ex)} folds")
    print(f"  AFFECTED teams only, excl-short: delta {d_aff:+.4f} +/- {se_aff:.4f} SE, "
          f"{(valid_aff['delta_affected'] > 0).sum()}/{len(valid_aff)} folds "
          f"(n_affected total: {int(ex['n_affected_base'].sum())} team-seasons)")
    verdict = "SHIP" if d_aff > 0.05 else "borderline / do not ship"
    print(f"\n  => {verdict}  (judged on the AFFECTED-team number -- the discount cannot")
    print(f"     move MAE for teams with no qualifying returning player)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
