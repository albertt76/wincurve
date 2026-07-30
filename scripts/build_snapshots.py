"""Build per-season projection bundles for the app: the last few seasons plus the upcoming
one, each as the model would have seen it *before that season started* (walk-forward), with
the actual result attached for the historical ones.

The current-season bundle (2026-27) is produced by scripts/project_current.py from the live
roster; this script produces the historical bundles from reconstructed opening-day rosters
and stitches everything into one `snapshots.json` the UI can switch between.

    python scripts/build_snapshots.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.aging import (  # noqa: E402
    aging_curves, build_transitions, project_next_season, replacement_level,
)
from nbaproj.availability import build_availability, build_features, fit_predict  # noqa: E402
from nbaproj.carryover import apply_carryover, fit_rho  # noqa: E402
from nbaproj.minutes import MINUTES_PER_GAME  # noqa: E402
from nbaproj.project import (  # noqa: E402
    calibrate_projected_ratings, project_team_ratings, roster_opening_day,
)
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, roster_turnover,
    simulate_season, turnover_sigma_multiplier,
)
from nbaproj.teams import FULL_SEASON_GAMES, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
HISTORICAL = [2021, 2022, 2023, 2024, 2025]   # 2021-22 .. 2025-26
RATING_GRID = np.arange(-12.0, 12.01, 1.5)
WINS_PER_PT = 2.38
N_SIMS = 12000


def build_historical(season: int, data: dict) -> dict:
    """Walk-forward projection bundle for a completed season, with actuals attached."""
    imp, pts, pgl, gl, ages, ts, rosters, coaches, abbr = (
        data[k] for k in ("imp", "pts", "pgl", "gl", "ages", "ts", "rosters",
                          "coaches", "abbr"))
    hist = imp[imp["season_start"] < season]
    rep = replacement_level(hist, "impact")

    # --- player talent, prior seasons only ---
    curves = aging_curves(build_transitions(hist, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    talent = project_next_season(hist, curves, target_season=season,
                                 skill="impact").set_index("player_id")["proj_impact"]

    # --- opening-day roster, minutes from prior season, availability projected ---
    base = roster_opening_day(rosters, pgl, season)
    if base.empty:
        return {}
    pos = rosters[rosters["season_start"] == season].drop_duplicates(
        ["team_id", "player_id"])[["team_id", "player_id", "POSITION", "EXP"]]
    base = base.merge(pos, on=["team_id", "player_id"], how="left")

    prev = pts[pts["season_start"] == season - 1].groupby(
        "player_id", as_index=False).agg(m=("minutes", "sum"), g=("games", "sum"))
    prev["prior_mpg"] = prev["m"] / prev["g"].clip(lower=1)
    base = base.merge(prev[["player_id", "prior_mpg"]], on="player_id", how="left")
    base["prior_mpg"] = base["prior_mpg"].fillna(8.0)
    base["proj_impact"] = base["player_id"].map(talent).fillna(rep)

    avail = fit_predict(build_features(build_availability(pts, ts), ages),
                        target_season=season)
    base = base.merge(avail[["player_id", "proj_availability"]], on="player_id",
                      how="left")
    base["proj_availability"] = base["proj_availability"].fillna(0.85)
    base["age"] = base["player_id"].map(_age_lookup(ages, season))
    names = imp[imp["season_start"] == season - 1].set_index(
        "player_id")["player_name"].to_dict()
    base["name"] = base["player_id"].map(names).fillna("")

    # --- team rating: aggregate DIRECTLY from the stored roster, exactly as the UI's
    # what-if editor recomputes it, so the unedited baseline reproduces the shown rating.
    # (Using project_team_ratings' own aggregate here instead would diverge from the
    # per-player values we store, by ~0.7 rating points.) Calibration still comes from the
    # walk-forward backtest.
    backtest = _walk_forward_ratings(imp, pts, pgl, ts, ages, rosters, season)
    slope, intercept = calibrate_projected_ratings(backtest, ts, target_season=season)
    ratings = _aggregate_from_roster(base, rep)
    ratings["pred_net_rating_dev"] = slope * ratings["agg_impact"] + intercept
    scored = backtest.copy()
    scored["pred_net_rating_dev"] = slope * scored["agg_impact"] + intercept
    ratings["season_start"] = season
    ratings = apply_carryover(ratings, scored, ts, target_season=season)
    rho = fit_rho(scored, ts, before_season=season)

    actual = ts[ts["season_start"] == season].copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual["net_rating"].mean()
    sigma_base = fit_rating_sigma(
        scored, actual[["team_id", "season_start", "net_rating_dev"]].assign(
            season_start=season), before_season=season)
    turnover = roster_turnover(pts, season_start=season)
    ratings = ratings.merge(turnover, on="team_id", how="left")
    ratings["new_minute_share"] = ratings["new_minute_share"].fillna(0.3)
    ratings["sigma"] = sigma_base * ratings["new_minute_share"].map(
        turnover_sigma_multiplier)

    games = int(actual["games"].mode().iloc[0]) if not actual.empty else FULL_SEASON_GAMES
    return _assemble(season, ratings, base, data, sigma_base, slope, intercept, rep,
                     rho, is_current=False, actual=actual, games=games)


def _aggregate_from_roster(base: pd.DataFrame, rep: float) -> pd.DataFrame:
    """Team aggregate impact computed exactly as the UI recomputes it: minutes = mpg x
    availability x 82, scaled down to the 240x82 budget when over, leftover at replacement.
    """
    budget = MINUTES_PER_GAME * FULL_SEASON_GAMES
    rows = []
    for tid, g in base.groupby("team_id"):
        mins = (g["prior_mpg"] * g["proj_availability"] * FULL_SEASON_GAMES).to_numpy()
        vals = g["proj_impact"].to_numpy(dtype=float)
        used = mins.sum()
        if used > budget:
            mins = mins * budget / used
            used = budget
        leftover = max(budget - used, 0.0)
        agg = (float(np.sum(mins * vals)) + leftover * rep) / budget
        rows.append({"team_id": tid, "agg_impact": agg})
    return pd.DataFrame(rows)


def _age_lookup(ages: pd.DataFrame, season: int) -> dict:
    a = ages[ages["season_start"] == season - 1].set_index("player_id")["age"]
    return a.to_dict()


def _walk_forward_ratings(imp, pts, pgl, ts, ages, rosters, before: int) -> pd.DataFrame:
    frames = []
    for s in range(2017, before + 1):
        r = project_team_ratings(imp, pts, pgl, ts, ages, target_season=s,
                                 mode="roster", team_rosters=rosters)
        if not r.empty:
            frames.append(r)
    return pd.concat(frames, ignore_index=True)


def _assemble(season, ratings, roster, data, sigma_base, slope, intercept, rep, rho,
              *, is_current, actual, games) -> dict:
    ts, abbr = data["ts"], data["abbr"]
    coaches = data["coaches"]
    budget = MINUTES_PER_GAME * games
    gl = data["gl"]

    schedule = extract_schedule(gl, season if not is_current else season - 1)
    hca, margin_sd = estimate_game_params(gl, before_season=season)
    sim, wins = simulate_season(ratings[["team_id", "pred_net_rating_dev"]], schedule,
                                hca=hca, margin_sd=margin_sd,
                                sigma_rating=float(ratings["sigma"].mean()),
                                n_sims=N_SIMS)
    rng = np.random.default_rng(7)
    order = {t: i for i, t in enumerate(sim["team_id"])}
    grid = {}
    for _, tr in ratings.iterrows():
        tid = int(tr["team_id"])
        col = wins[:, order[tid]].astype(float) * FULL_SEASON_GAMES / games
        game_sd = np.sqrt(max(col.std()**2 - (WINS_PER_PT * sigma_base)**2, 1.0))
        pts_grid = []
        for off in RATING_GRID:
            sd_w = np.sqrt(game_sd**2 + (WINS_PER_PT * tr["sigma"])**2)
            draws = rng.normal(col.mean() + WINS_PER_PT * off, sd_w,
                               4000).clip(0, FULL_SEASON_GAMES)
            pts_grid.append({"offset": round(float(off), 2),
                             "mean": round(float(draws.mean()), 1),
                             "p10": round(float(np.percentile(draws, 10)), 1),
                             "p25": round(float(np.percentile(draws, 25)), 1),
                             "p75": round(float(np.percentile(draws, 75)), 1),
                             "p90": round(float(np.percentile(draws, 90)), 1)})
        grid[tid] = pts_grid

    name_map = ts[ts["season_start"] == season - 1].set_index(
        "team_id")[["team", "team_name"]].to_dict("index") if season > 2005 else {}
    hc = coaches[coaches["COACH_TYPE"] == "Head Coach"].copy()
    hc["team_id"] = hc["TEAM_ID"].astype("int64")
    hc["ss"] = hc["SEASON_START"].astype(int)
    coach_map = hc[hc["ss"] == season].drop_duplicates("team_id").set_index(
        "team_id")["COACH_NAME"].to_dict()
    actual_wins = actual.set_index("team_id")["wins_actual"].to_dict() \
        if actual is not None else {}
    actual_w82 = (actual.set_index("team_id")["win_pct"] * FULL_SEASON_GAMES).to_dict() \
        if actual is not None else {}

    teams = []
    for _, tr in ratings.iterrows():
        tid = int(tr["team_id"])
        g = roster[roster["team_id"] == tid].sort_values("prior_mpg", ascending=False)
        players = [{
            "id": int(p["player_id"]), "name": str(p.get("name", "")),
            "pos": str(p["POSITION"]) if pd.notna(p.get("POSITION")) else "",
            "age": int(p["age"]) if pd.notna(p.get("age")) else None,
            "impact": round(float(p["proj_impact"]), 5),
            "mpg": round(float(p["prior_mpg"]), 4),
            "avail": round(float(p["proj_availability"]), 5),
            "rookie": False, "override": False,
        } for _, p in g.iterrows()]
        base = grid[tid][len(RATING_GRID) // 2]
        rec = {
            "id": tid, "abbr": abbr.get(tid, name_map.get(tid, {}).get("team", "?")),
            "name": name_map.get(tid, {}).get("team_name", "?"),
            "coach": coach_map.get(tid, "unknown"),
            "agg_impact": round(float(tr["agg_impact"]), 4),
            "rating": round(float(tr["pred_net_rating_dev"]), 2),
            "carryover": round(float(tr.get("carryover", 0.0)), 2),
            "sigma": round(float(tr["sigma"]), 3),
            "turnover": round(float(tr["new_minute_share"]), 3),
            "wins": base["mean"], "p10": base["p10"], "p25": base["p25"],
            "p75": base["p75"], "p90": base["p90"], "players": players,
        }
        if not is_current:
            rec["actual"] = round(float(actual_w82.get(tid, np.nan)), 1) \
                if tid in actual_w82 else None
        teams.append(rec)
    teams.sort(key=lambda t: -t["wins"])

    # Backtest accuracy of THIS season's projection.
    mae = None
    if not is_current:
        diffs = [abs(t["wins"] - t["actual"]) for t in teams if t.get("actual") is not None]
        mae = round(float(np.mean(diffs)), 2) if diffs else None

    return {
        "season": f"{season}-{str(season + 1)[-2:]}",
        "season_start": season,
        "is_current": is_current,
        "rating_slope": round(slope, 4),
        "rating_intercept": round(intercept, 4),
        "sigma_base": round(sigma_base, 3),
        "rho_carryover": round(rho, 3),
        "season_mae": mae,
        "teams": teams,
        "grid": {str(k): v for k, v in grid.items()},
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    from nba_api.stats.static import teams as static_teams
    data = {
        "imp": pd.read_parquet(PROC / "player_impact.parquet"),
        "pts": pd.read_parquet(PROC / "player_team_seasons.parquet"),
        "pgl": pd.read_parquet(PROC / "player_game_log.parquet"),
        "gl": pd.read_parquet(PROC / "game_log.parquet"),
        "ts": load_team_seasons(),
        "rosters": pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
            "TeamID": "team_id", "PLAYER_ID": "player_id",
            "SEASON_START": "season_start"}),
        "coaches": pd.read_parquet(PROC / "team_coaches.parquet"),
        "abbr": {t["id"]: t["abbreviation"] for t in static_teams.get_teams()},
    }
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    data["ages"] = pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()

    snapshots = {}
    for season in HISTORICAL:
        b = build_historical(season, data)
        if b:
            snapshots[b["season"]] = b
            print(f"  {b['season']}: MAE {b['season_mae']}  "
                  f"({len(b['teams'])} teams, rho {b['rho_carryover']})")

    # Current season from the existing live bundle.
    cur_path = PROC / "projections_current.json"
    if cur_path.exists():
        cur = json.loads(cur_path.read_text())
        key = cur["meta"]["season"]
        snapshots[key] = {
            "season": key, "season_start": int(key[:4]), "is_current": True,
            "snapshot_date": cur["meta"].get("snapshot_date"),
            "rating_slope": cur["meta"].get("rating_slope"),
            "rating_intercept": cur["meta"].get("rating_intercept"),
            "sigma_base": cur["meta"].get("sigma_base"),
            "rho_carryover": cur["meta"].get("rho_carryover"),
            "season_mae": None, "teams": cur["teams"], "grid": cur["grid"],
        }
        print(f"  {key}: current (live snapshot {cur['meta'].get('snapshot_date')})")

    out = {
        "meta": {
            "minutes_budget": 240.0 * FULL_SEASON_GAMES,
            "full_season_games": FULL_SEASON_GAMES,
            "replacement_impact": round(float(
                replacement_level(data["imp"], "impact")), 3),
            "wins_per_rating_point": WINS_PER_PT,
            "market_mae": 6.88,
            "seasons": sorted(snapshots.keys()),
        },
        "snapshots": snapshots,
    }
    path = PROC / "snapshots.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    print(f"\nwrote {path}  ({path.stat().st_size // 1024} KB, "
          f"{len(snapshots)} seasons)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
