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
import re
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
from nbaproj.awards import honor_lookup, load_honors  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import box_vs_rapm_by_player, build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import (  # noqa: E402
    backtest_aggregates, blend_weight, calibrate_blend, project_rapm_def,
)
from nbaproj.rosters import (  # noqa: E402
    apply_overrides, apply_return_overrides, draft_history, fit_rookie_priors,
    injured_season_mask, load_overrides, load_return_overrides,
    resolve_return_overrides, rookie_projection,
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


def _grid_mean_wins(grid: list[dict], offset: float) -> float:
    """Interpolate mean wins from a team's rating-offset grid (mirrors the UI's winsAt)."""
    if offset <= grid[0]["offset"]:
        return grid[0]["mean"]
    if offset >= grid[-1]["offset"]:
        return grid[-1]["mean"]
    for a, b in zip(grid, grid[1:]):
        if a["offset"] <= offset <= b["offset"]:
            f = (offset - a["offset"]) / (b["offset"] - a["offset"])
            return a["mean"] + f * (b["mean"] - a["mean"])
    return grid[len(grid) // 2]["mean"]


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
    # --- injury-return overrides (inverse of known_absences): stars back from a
    #     season-long injury, projected AS their last healthy season. A returning star's
    #     post-injury partial seasons are dropped from the projection INPUTS, so the normal
    #     aging + shrinkage restore his pre-injury offense/defense (box AND RAPM) instead of
    #     us hand-entering numbers; his minutes and availability are restored later, after
    #     the roster merge. Live bundle only -- these never touch the walk-forward backtest.
    #     Filtering only fires for a basis within two seasons of TARGET (else the reprojected
    #     player would fail project_next_season's "seen recently" gate and fall to a rookie
    #     prior); an older basis keeps its natural minute-weighted projection, which already
    #     leans on the last healthy season. Minutes/availability still restore for any basis.
    returns_raw = load_return_overrides()
    returns = resolve_return_overrides(returns_raw, imp)
    returns_recent = returns[returns["basis_season"] >= TARGET - 2] if not returns.empty \
        else returns
    imp_proj = imp[~injured_season_mask(imp, returns_recent)]

    curves = aging_curves(build_transitions(imp, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    proj = project_next_season(imp_proj, curves, target_season=TARGET, skill="impact")
    proj_off = project_next_season(imp_proj, curves, target_season=TARGET,
                                   skill="off_impact")
    proj_def = project_next_season(imp_proj, curves, target_season=TARGET,
                                   skill="def_impact")
    rep = replacement_level(imp, "impact")
    rep_off = replacement_level(imp, "off_impact")
    rep_def = replacement_level(imp, "def_impact")
    # RAPM-defense arm: the same defensive projection, but valued by play-by-play RAPM wherever
    # available. Each team's defense blends the box and RAPM aggregates by roster turnover, so
    # a turned-over roster (whose defense the carryover can't follow) leans on RAPM. Worth
    # +0.19 wins walk-forward, all folds -- see nbaproj.rapm_blend / scripts/gate_rapm_blend.py.
    rapm_imp = build_rapm_impact(imp, PROC)
    proj_def_rapm = project_rapm_def(
        rapm_imp[~injured_season_mask(rapm_imp, returns_recent)], TARGET)
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

    # Each player's PRIOR team, for the "came from X" arrival label + one-click undo. Use the team
    # he was on at the END of last season (his latest game), NOT his primary team by minutes: a
    # player traded mid-last-season (e.g. Vučević CHI->BOS, D'Angelo Russell DAL->WAS) logged more
    # minutes with his OLD team but was LAST on -- and this offseason left -- the newer one, so
    # latest-by-date is who actually lost him. For trades, HOW_ACQUIRED's explicit "from" team
    # overrides this at emission (authoritative even after a 0-minute deadline stint). This is
    # roster MOVEMENT (trades + free agency + waivers) -- transaction type is not in any free feed,
    # so it is deliberately not labelled "trades" only.
    _pl = pgl[pgl["SEASON_START"] == LAST_HISTORY][["PLAYER_ID", "TEAM_ID", "GAME_DATE"]].copy()
    _pl["GAME_DATE"] = pd.to_datetime(_pl["GAME_DATE"], errors="coerce")
    _pl = _pl.dropna(subset=["GAME_DATE"])
    prev_team = (_pl.loc[_pl.groupby("PLAYER_ID")["GAME_DATE"].idxmax(), ["PLAYER_ID", "TEAM_ID"]]
                 .rename(columns={"PLAYER_ID": "player_id", "TEAM_ID": "prev_team_id"}))
    prev_team["player_id"] = prev_team["player_id"].astype("int64")
    prev_team["prev_team_id"] = prev_team["prev_team_id"].astype("int64")

    roster = cur_rosters[["team_id", "player_id", "PLAYER", "POSITION", "AGE",
                          "EXP", "HOW_ACQUIRED"]].drop_duplicates(["team_id", "player_id"])
    roster = roster.merge(prev[["player_id", "prior_mpg"]], on="player_id", how="left")
    roster = roster.merge(prev_team, on="player_id", how="left")

    # Restrict the moves panel to TRUE offseason moves and exclude last season's mid-season
    # (trade-deadline / buyout) movement. Latest-by-date already drops most such cases (a player
    # acquired mid-last-season usually ends the season on -- so points prev at -- his current team),
    # but NOT one acquired at the deadline who then logged 0 minutes: his last game is still for his
    # OLD team, so prev != current and he looks like a fresh arrival -- e.g. Anthony Davis ("Traded
    # from DAL on 02/05/26", injured after the deal). Ground truth is the acquisition DATE in
    # HOW_ACQUIRED: on/after OFFSEASON_START (May 1 of the target year, after last season ends) it
    # is a real offseason move; earlier it happened during/before last season. When HOW_ACQUIRED
    # carries no parseable date, fall back to minute overlap -- a player who logged minutes with his
    # CURRENT team last season was already here mid-season, so it is not an offseason arrival.
    OFFSEASON_START = pd.Timestamp(f"{TARGET}-05-01")
    last_team_min = set(zip(pts.loc[pts["season_start"] == LAST_HISTORY, "player_id"],
                            pts.loc[pts["season_start"] == LAST_HISTORY, "team_id"]))
    acq_date = pd.to_datetime(
        roster["HOW_ACQUIRED"].fillna("").astype(str).str.extract(r"(\d{2}/\d{2}/\d{2})")[0],
        format="%m/%d/%y", errors="coerce")
    overlap = np.array([(pid, tid) in last_team_min
                        for pid, tid in zip(roster["player_id"], roster["team_id"])])
    roster["offseason_move"] = np.where(acq_date.notna().to_numpy(),
                                        (acq_date >= OFFSEASON_START).to_numpy(),
                                        ~overlap)
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
    # Injury returns: restore the returning star's role (prior_mpg from the basis season,
    # times any eased-in minute_restriction) and set his availability to the expected value.
    # Applied AFTER the absence floor so an explicit return wins for the same player.
    roster = apply_return_overrides(roster, returns)

    # --- calibration and uncertainty, fitted on history. Decoupled offense/defense; the
    #     defensive aggregate blends the box and RAPM arms by roster turnover, and the defensive
    #     slope is calibrated on that blend (nbaproj.rapm_blend). ---
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, hist_rosters,
                            range(2017, LAST_HISTORY + 1))
    cal = calibrate_blend(A, ts, target_season=TARGET)
    off_slope, off_int = cal["off_slope"], cal["off_intercept"]
    def_slope, def_int = cal["def_slope"], cal["def_intercept"]
    slope, intercept = cal["rating_slope"], cal["rating_intercept"]
    # A second, RAPM-ONLY defensive calibration for the display arm: the same offense, but
    # defense priced purely from play-by-play RAPM (its own, lower slope ~3.3 since the RAPM
    # aggregate is ~2x more dispersed). Shown beside the shipped blend so a reader can see how
    # much the defensive-metric choice moves each team; it is DISPLAY ONLY -- re-fitting the
    # blend toward it did not clear the gate (scripts/gate_blend_weight.py). The two arms
    # diverge most on stable rosters, where the blend ~= box and RAPM's perimeter read differs.
    def_slope_rapm, def_int_rapm = calibrate_projected_ratings(
        A, ts, target_season=TARGET, target="def_rating", agg_col="agg_def_rapm")

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

    # RAPM-only display arm: same offense and same carryover, defense from pure RAPM. Isolating
    # the defensive-metric choice, so rating_rapm - rating is exactly the (RAPM - blend) defense.
    teams["def_rating_rapm"] = def_slope_rapm * teams["agg_def_rapm"] + def_int_rapm
    teams["rating_rapm"] = (teams["off_rating"] + teams["def_rating_rapm"]
                            + teams["carryover"])

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

    # Eye-test honors (All-Defensive / All-NBA), most recent selection before TARGET. A display
    # layer only -- prior-year honors did not beat the win gate (see nbaproj/awards.py), so they
    # never touch the projection; they annotate players the metric may disagree with.
    honors = honor_lookup(load_honors(PROC), before_season=TARGET)

    # --- assemble the bundle ---
    # team_advanced carries no TEAM_ABBREVIATION column, so nbaproj.teams falls back to
    # the full name. Pull real abbreviations from the static team list instead.
    from nba_api.stats.static import teams as static_teams
    abbr_map = {t["id"]: t["abbreviation"] for t in static_teams.get_teams()}
    abbr_to_id = {v: k for k, v in abbr_map.items()}  # for resolving HOW_ACQUIRED "from XXX"
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
            "def_slope_rapm": round(def_slope_rapm, 4),
            "def_intercept_rapm": round(def_int_rapm, 4),
            "sigma_base": round(sigma_base, 3),
            "replacement_impact": round(rep, 3),
            "replacement_off": round(rep_off, 4),
            "replacement_def": round(rep_def, 4),
            "replacement_def_rapm": round(rep_def_rapm, 4),
            "minutes_budget": budget,
            "full_season_games": FULL_SEASON_GAMES,
            "wins_per_rating_point": 2.38,
            "rho_carryover": round(rho_used, 3),
            "backtest_mae": 7.58,
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
                "ret_override": bool(p.get("has_return_override", False)),
            }
            if p.get("has_return_override", False):
                rec["ret_reason"] = str(p.get("return_reason", ""))
            pti = p.get("prev_team_id")
            # true offseason arrival from another team -- NOT last season's deadline/buyout move
            if pd.notna(pti) and int(pti) != tid and bool(p.get("offseason_move", False)):
                # Prefer the actual prior team named in HOW_ACQUIRED for a trade: primary-team-by-
                # minutes can point at a DIFFERENT team when the player was ALSO traded mid-last-
                # season -- e.g. D'Angelo Russell was "Traded from WAS" this offseason but logged
                # more minutes at DAL last season, so "came from WAS" (not DAL) is correct, and it
                # is WAS (not DAL) that should list him as an offseason departure.
                mm = re.search(r"from ([A-Z]{3})", str(p.get("HOW_ACQUIRED") or ""))
                from_id = abbr_to_id.get(mm.group(1)) if mm else None
                src = from_id if (from_id is not None and from_id != tid) else int(pti)
                rec["prev"] = abbr_map.get(src, name_map.get(src, {}).get("team", "?"))
                rec["prevId"] = src
            dc = def_cmp.get(int(p["player_id"]))
            if dc:
                rec["box_def"] = round(float(dc["box_def"]), 3)
                rec["rapm_def"] = round(float(dc["def_rapm"]), 3)
                rec["rapm_yr"] = int(dc["season_start"])
            ad = honors["all_def"].get(int(p["player_id"]))
            if ad:
                rec["all_def"] = ad          # {yr, team (1/2), n = career count}
            an = honors["all_nba"].get(int(p["player_id"]))
            if an:
                rec["all_nba"] = an
            return rec
        players = [_player(p) for _, p in g.iterrows()]

        base = grid_out[tid][len(RATING_GRID) // 2]
        rating_rapm = float(tr["rating_rapm"])
        out["teams"].append({
            "id": tid,
            "abbr": abbr_map.get(tid, name_map.get(tid, {}).get("team", "?")),
            "name": name_map.get(tid, {}).get("team_name", "?"),
            "coach": coach_map.get(tid, "unknown"),
            "agg_impact": round(float(tr["agg_impact"]), 4),
            "rating": round(float(tr["pred_net_rating_dev"]), 2),
            "off_rating": round(float(tr["off_rating"]), 2),
            "def_rating": round(float(tr["def_rating"]), 2),
            # RAPM-only display arm (defense from pure RAPM, same offense + carryover).
            "rating_rapm": round(rating_rapm, 2),
            "def_rating_rapm": round(float(tr["def_rating_rapm"]), 2),
            "wins_rapm": round(_grid_mean_wins(
                grid_out[tid], rating_rapm - float(tr["pred_net_rating_dev"])), 1),
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
