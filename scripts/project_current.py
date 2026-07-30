"""Generate live projections for the upcoming season, and the UI data bundle.

Emits `data/processed/projections_current.json`, which the UI consumes. It carries not
just the team numbers but the per-player pieces, so the interface can add and remove
players and recompute a team's projection locally -- the aggregation is linear, so that is
exact rather than an approximation.

    python scripts/project_current.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.aging import (  # noqa: E402
    aging_curves, build_transitions, project_next_season, replacement_level,
)
from nbaproj.carryover import apply_carryover, fit_rho  # noqa: E402
from nbaproj.rapm import box_vs_rapm_by_player, build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import (  # noqa: E402
    backtest_aggregates, blend_weight, calibrate_blend, project_rapm_def,
)
from nbaproj.rosters import (  # noqa: E402
    apply_overrides, draft_history, fit_rookie_priors, load_overrides,
    rookie_projection,
)
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, roster_turnover,
    simulate_season, turnover_sigma_multiplier,
)
from nbaproj.teams import FULL_SEASON_GAMES, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
TARGET = 2026          # 2026-27 season
LAST_HISTORY = 2025    # last completed season
N_SIMS = 20000

# Grid of own-rating offsets simulated per team, so the UI can interpolate the win
# distribution for a hypothetical roster instead of re-running a simulation in the browser.
RATING_GRID = np.arange(-12.0, 12.01, 1.5)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    hist_rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    cur_rosters = pd.read_parquet(PROC / "rosters_current.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    cur_coaches = pd.read_parquet(PROC / "coaches_current.parquet")
    ts = load_team_seasons()

    ages = pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce"),
    }).drop_duplicates()

    # --- player talent, from completed seasons only. Offense and defense are projected and
    #     aggregated separately (decoupled): net-neutral on accuracy, but it lets us attribute
    #     a team's rating to its offense vs its defense, and surface where our (weak) box-score
    #     defense disagrees with play-by-play RAPM. ---
    curves = aging_curves(build_transitions(imp, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    proj = project_next_season(imp, curves, target_season=TARGET, skill="impact")
    proj_off = project_next_season(imp, curves, target_season=TARGET, skill="off_impact")
    proj_def = project_next_season(imp, curves, target_season=TARGET, skill="def_impact")
    rep = replacement_level(imp, "impact")
    rep_off = replacement_level(imp, "off_impact")
    rep_def = replacement_level(imp, "def_impact")
    # RAPM-defense arm: the same defensive projection, but valued by play-by-play RAPM wherever
    # available. Each team's defense blends the box and RAPM aggregates by roster turnover, so
    # a turned-over roster (whose defense the carryover can't follow) leans on RAPM. Worth
    # +0.19 wins walk-forward, all folds -- see nbaproj.rapm_blend / scripts/gate_rapm_blend.py.
    rapm_imp = build_rapm_impact(imp, PROC)
    proj_def_rapm = project_rapm_def(rapm_imp, TARGET)
    rep_def_rapm = replacement_level(rapm_imp, "def_impact")

    # --- rookies: no NBA record, so priors by draft slot ---
    draft = draft_history()
    priors = fit_rookie_priors(imp, draft, before_season=TARGET)
    draft["pick"] = pd.to_numeric(draft["OVERALL_PICK"], errors="coerce")
    draft["yr"] = pd.to_numeric(draft["SEASON"], errors="coerce")
    rookie_picks = draft[draft["yr"] == TARGET][["PERSON_ID", "pick"]].rename(
        columns={"PERSON_ID": "player_id"})
    rookie_picks["player_id"] = rookie_picks["player_id"].astype("int64")

    # --- prior-season minutes, for role and turnover ---
    prev = pts[pts["season_start"] == LAST_HISTORY].groupby(
        "player_id", as_index=False).agg(m=("minutes", "sum"), g=("games", "sum"))
    prev["prior_mpg"] = prev["m"] / prev["g"].clip(lower=1)

    roster = cur_rosters[["team_id", "player_id", "PLAYER", "POSITION", "AGE",
                          "EXP"]].drop_duplicates(["team_id", "player_id"])
    roster = roster.merge(prev[["player_id", "prior_mpg"]], on="player_id", how="left")
    roster = roster.merge(proj[["player_id", "proj_impact"]], on="player_id", how="left")
    roster = roster.merge(proj_off[["player_id", "proj_off_impact"]], on="player_id",
                          how="left")
    roster = roster.merge(proj_def[["player_id", "proj_def_impact"]], on="player_id",
                          how="left")
    roster = roster.merge(proj_def_rapm.rename("proj_def_rapm").reset_index(),
                          on="player_id", how="left")
    roster = roster.merge(rookie_picks, on="player_id", how="left")

    # Rookies and other players with no projection fall back to a draft-slot prior.
    need = roster["proj_impact"].isna()
    for idx in roster.index[need]:
        pk = roster.at[idx, "pick"]
        pr = rookie_projection(priors, pk if pd.notna(pk) else None)
        roster.at[idx, "proj_impact"] = pr["exp_impact"]
        roster.at[idx, "proj_off_impact"] = pr["exp_off_impact"]
        roster.at[idx, "proj_def_impact"] = pr["exp_def_impact"]
        if pd.isna(roster.at[idx, "prior_mpg"]):
            roster.at[idx, "prior_mpg"] = pr["exp_minutes"] / FULL_SEASON_GAMES
    roster["prior_mpg"] = roster["prior_mpg"].fillna(8.0)
    # Any residual component gaps fall to replacement level, split into off/def parts. A player
    # with no RAPM estimate (rookie, sparse minutes) falls back to his box defense.
    roster["proj_off_impact"] = roster["proj_off_impact"].fillna(rep_off)
    roster["proj_def_impact"] = roster["proj_def_impact"].fillna(rep_def)
    roster["proj_def_rapm"] = roster["proj_def_rapm"].fillna(roster["proj_def_impact"])
    roster["is_rookie"] = need & roster["pick"].notna()

    # --- availability, with manual overrides for known absences ---
    from nbaproj.availability import build_availability, build_features, fit_predict
    avail = build_availability(pts, ts)
    feats = build_features(avail, ages)
    av = fit_predict(feats, target_season=TARGET)
    roster = roster.merge(av[["player_id", "proj_availability"]], on="player_id",
                          how="left")
    roster["proj_availability"] = roster["proj_availability"].fillna(0.85)
    roster = apply_overrides(roster, load_overrides(), team_games=FULL_SEASON_GAMES)

    # --- calibration and uncertainty, fitted on history. Decoupled offense/defense; the
    #     defensive aggregate blends the box and RAPM arms by roster turnover, and the defensive
    #     slope is calibrated on that blend (nbaproj.rapm_blend). ---
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, hist_rosters,
                            range(2017, LAST_HISTORY + 1))
    cal = calibrate_blend(A, ts, target_season=TARGET)
    off_slope, off_int = cal["off_slope"], cal["off_intercept"]
    def_slope, def_int = cal["def_slope"], cal["def_intercept"]
    slope, intercept = cal["rating_slope"], cal["rating_intercept"]

    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    scored = A.copy()
    scored["pred_net_rating_dev"] = (off_slope * scored["agg_off"] + off_int
                                     + def_slope * scored["agg_def_used"] + def_int)
    sigma_base = fit_rating_sigma(
        scored, actual[["team_id", "season_start", "net_rating_dev"]],
        before_season=TARGET + 1)

    turnover = roster_turnover(pts, season_start=TARGET, roster=roster)
    hca, margin_sd = estimate_game_params(gl, before_season=TARGET)

    # --- aggregate each team: offense, and defense both ways (box and RAPM). The defensive
    #     aggregate actually used is the turnover-weighted blend -- a steady roster leans on the
    #     box metric (which the carryover corrects), a turned-over one on RAPM whose value
    #     follows the incoming players. off_rating + def_rating is the roster's net rating. ---
    budget = 240.0 * FULL_SEASON_GAMES
    team_rows = []
    for tid, g in roster.groupby("team_id"):
        mins = (g["prior_mpg"] * g["proj_availability"] * FULL_SEASON_GAMES).to_numpy()
        off_vals = g["proj_off_impact"].to_numpy(dtype=float)
        def_vals = g["proj_def_impact"].to_numpy(dtype=float)
        def_rapm_vals = g["proj_def_rapm"].to_numpy(dtype=float)
        used = mins.sum()
        if used > budget:
            mins = mins * budget / used
            used = budget
        leftover = max(budget - used, 0.0)
        agg_off = (float(np.sum(mins * off_vals)) + leftover * rep_off) / budget
        agg_def_box = (float(np.sum(mins * def_vals)) + leftover * rep_def) / budget
        agg_def_rapm = (float(np.sum(mins * def_rapm_vals)) + leftover * rep_def_rapm) / budget
        team_rows.append({"team_id": tid, "agg_off": agg_off, "agg_def_box": agg_def_box,
                          "agg_def_rapm": agg_def_rapm, "leftover_share": leftover / budget})
    teams = pd.DataFrame(team_rows).merge(turnover, on="team_id", how="left")
    w = blend_weight(teams["new_minute_share"])
    teams["agg_def_used"] = (1 - w) * teams["agg_def_box"] + w * teams["agg_def_rapm"]
    teams["agg_impact"] = teams["agg_off"] + teams["agg_def_used"]
    teams["off_rating"] = off_slope * teams["agg_off"] + off_int
    teams["def_rating"] = def_slope * teams["agg_def_used"] + def_int
    teams["pred_net_rating_dev"] = teams["off_rating"] + teams["def_rating"]

    # One-year residual carryover: the model's error on a team persists ~one season, so
    # add rho * last season's residual. rho is fitted on prior residual pairs only, and
    # the guard suppresses it after a shortened prior season. LAST_HISTORY (2025) is full,
    # so it fires here. See nbaproj.carryover.
    teams["season_start"] = TARGET
    teams = apply_carryover(teams, scored, ts, target_season=TARGET)
    rho_used = fit_rho(scored, ts, before_season=TARGET)

    teams["sigma"] = sigma_base * teams["new_minute_share"].map(
        turnover_sigma_multiplier)

    # --- simulate, using last season's schedule as the structural stand-in ---
    schedule = extract_schedule(gl, LAST_HISTORY)
    sim, wins = simulate_season(
        teams[["team_id", "pred_net_rating_dev"]], schedule,
        hca=hca, margin_sd=margin_sd,
        sigma_rating=float(teams["sigma"].mean()), n_sims=N_SIMS)

    # Per-team sigma is applied by re-simulating each team's own uncertainty around its
    # mean, holding opponents fixed -- cheaper than 30 separate full simulations and
    # equivalent for a single team's marginal distribution.
    rng = np.random.default_rng(7)
    order = {t: i for i, t in enumerate(sim["team_id"])}
    grid_out = {}
    for _, tr in teams.iterrows():
        tid = int(tr["team_id"])
        col = wins[:, order[tid]].astype(float)
        # Re-centre and re-scale the simulated distribution for this team's own sigma.
        base_sd = col.std()
        game_sd = np.sqrt(max(base_sd**2 - (2.7 * sigma_base)**2, 1.0))
        pts_grid = []
        for off in RATING_GRID:
            mean_w = col.mean() + 2.38 * off
            sd_w = np.sqrt(game_sd**2 + (2.38 * tr["sigma"])**2)
            draws = rng.normal(mean_w, sd_w, 4000).clip(0, FULL_SEASON_GAMES)
            pts_grid.append({
                "offset": round(float(off), 2),
                "mean": round(float(draws.mean()), 1),
                "p10": round(float(np.percentile(draws, 10)), 1),
                "p25": round(float(np.percentile(draws, 25)), 1),
                "p75": round(float(np.percentile(draws, 75)), 1),
                "p90": round(float(np.percentile(draws, 90)), 1),
            })
        grid_out[tid] = pts_grid

    # --- per-player defensive disagreement: our box-score defense vs play-by-play RAPM,
    #     from the most recent season where we have both. RAPM sees perimeter defense the box
    #     score cannot (the box metric is ~60% rebounds+blocks, so it mostly rewards bigs), so
    #     a large positive (rapm - box) flags a defender our metric underrates. ---
    def_cmp = box_vs_rapm_by_player(imp, PROC, last_season=LAST_HISTORY)

    # --- assemble the bundle ---
    # team_advanced carries no TEAM_ABBREVIATION column, so nbaproj.teams falls back to
    # the full name. Pull real abbreviations from the static team list instead.
    from nba_api.stats.static import teams as static_teams
    abbr_map = {t["id"]: t["abbreviation"] for t in static_teams.get_teams()}
    name_map = ts[ts["season_start"] == LAST_HISTORY].set_index(
        "team_id")[["team", "team_name"]].to_dict("index")
    hc = cur_coaches[cur_coaches["COACH_TYPE"] == "Head Coach"].copy()
    hc["team_id"] = hc["TEAM_ID"].astype("int64")
    coach_map = hc.drop_duplicates("team_id").set_index(
        "team_id")["COACH_NAME"].to_dict()

    out = {
        "meta": {
            "season": f"{TARGET}-{str(TARGET + 1)[-2:]}",
            "snapshot_date": date.today().isoformat(),
            "rating_slope": round(slope, 3),
            "rating_intercept": round(intercept, 3),
            "off_slope": round(off_slope, 4),
            "off_intercept": round(off_int, 4),
            "def_slope": round(def_slope, 4),
            "def_intercept": round(def_int, 4),
            "sigma_base": round(sigma_base, 3),
            "replacement_impact": round(rep, 3),
            "replacement_off": round(rep_off, 4),
            "replacement_def": round(rep_def, 4),
            "replacement_def_rapm": round(rep_def_rapm, 4),
            "minutes_budget": budget,
            "full_season_games": FULL_SEASON_GAMES,
            "wins_per_rating_point": 2.38,
            "rho_carryover": round(rho_used, 3),
            "backtest_mae": 7.74,
            "market_mae": 6.88,
        },
        "teams": [],
        "grid": {str(k): v for k, v in grid_out.items()},
    }

    for _, tr in teams.iterrows():
        tid = int(tr["team_id"])
        g = roster[roster["team_id"] == tid].copy()
        g = g.sort_values("prior_mpg", ascending=False)

        def _player(p: pd.Series) -> dict:
            rec = {
                "id": int(p["player_id"]),
                "name": str(p["PLAYER"]),
                "pos": str(p["POSITION"]) if pd.notna(p["POSITION"]) else "",
                "age": int(p["AGE"]) if pd.notna(p["AGE"]) else None,
                "impact": round(float(p["proj_impact"]), 5),
                "off": round(float(p["proj_off_impact"]), 5),
                "def": round(float(p["proj_def_impact"]), 5),
                "defr": round(float(p["proj_def_rapm"]), 5),
                "mpg": round(float(p["prior_mpg"]), 4),
                "avail": round(float(p["proj_availability"]), 5),
                "rookie": bool(p["is_rookie"]),
                "override": bool(p.get("has_override", False)),
            }
            dc = def_cmp.get(int(p["player_id"]))
            if dc:
                rec["box_def"] = round(float(dc["box_def"]), 3)
                rec["rapm_def"] = round(float(dc["def_rapm"]), 3)
                rec["rapm_yr"] = int(dc["season_start"])
            return rec
        players = [_player(p) for _, p in g.iterrows()]

        base = grid_out[tid][len(RATING_GRID) // 2]
        out["teams"].append({
            "id": tid,
            "abbr": abbr_map.get(tid, name_map.get(tid, {}).get("team", "?")),
            "name": name_map.get(tid, {}).get("team_name", "?"),
            "coach": coach_map.get(tid, "unknown"),
            "agg_impact": round(float(tr["agg_impact"]), 4),
            "rating": round(float(tr["pred_net_rating_dev"]), 2),
            "off_rating": round(float(tr["off_rating"]), 2),
            "def_rating": round(float(tr["def_rating"]), 2),
            "carryover": round(float(tr.get("carryover", 0.0)), 2),
            "sigma": round(float(tr["sigma"]), 3),
            "turnover": round(float(tr["new_minute_share"]), 3),
            "wins": base["mean"],
            "p10": base["p10"], "p25": base["p25"],
            "p75": base["p75"], "p90": base["p90"],
            "players": players,
        })

    out["teams"].sort(key=lambda t: -t["wins"])
    path = PROC / "projections_current.json"
    path.write_text(json.dumps(out, indent=1))

    print(f"{out['meta']['season']} projections, snapshot {out['meta']['snapshot_date']}")
    print(f"slope {slope:.2f}  sigma_base {sigma_base:.2f}  replacement {rep:.2f}\n")
    print(f"{'team':<5} {'wins':>5} {'80% range':>12} {'turnover':>9} {'sigma':>6} coach")
    for t in out["teams"]:
        print(f"{t['abbr']:<5} {t['wins']:>5.1f} {t['p10']:>5.1f}-{t['p90']:<6.1f} "
              f"{t['turnover']:>9.2f} {t['sigma']:>6.2f} {t['coach']}")
    print(f"\nsum of projected wins = {sum(t['wins'] for t in out['teams']):.0f} "
          f"(should be ~1230)")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
