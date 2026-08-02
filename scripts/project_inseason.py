"""Produce a REST-OF-SEASON (in-season) projection for a season that is under way.

Run this at your in-season checkpoints (~25 games, ~50 games post trade deadline). It shrinks each
team's schedule-adjusted in-season rating toward its preseason projection by the walk-forward-fit
weight w(N), simulates the remaining schedule, and adds the banked wins. Gated in
`scripts/gate_inseason_model.py` (beats preseason-carried-forward at both splits, and naive pace at
N=25). See nbaproj/inseason.py.

    python scripts/project_inseason.py --season 2024 --games 25    # demo/backtest on a past season
    python scripts/project_inseason.py --season 2026 --games 25    # live, once 2026-27 games exist

Prior source:
- past season: the shipped WALK-FORWARD preseason projection (backtest pipeline).
- upcoming season (2026): the live `projections_current.json` ratings.
Remaining schedule: the real post-split games from game_log if present (exact); otherwise the prior
season's schedule as a structural stand-in (a live mid-season run before a schedule pull), flagged.

Emits `data/processed/projection_inseason.json` (per-team banked / projected-remaining / projected-
full wins, w, obs vs prior rating). Feed it to `scripts/log_projection.py --model inseason` to add
it to the drift time-series.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.inseason import blend_rating, fit_w, through_split  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, simulate_season)
from nbaproj.teams import FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
TARGET = 2026
N_SIMS = 20000
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}


def _prior_pipeline(imp, pts, pgl, ts, ages, rosters, last):
    """cal frame of preseason pred_net_rating_dev (pre-carryover) + actual2 for sigma fitting."""
    rapm_imp = build_rapm_impact(imp, PROC)
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, range(2016, last + 1))
    cal = []
    for s in sorted(A.season_start.unique()):
        so, io = calibrate_projected_ratings(A, ts, target_season=s, target="off_rating",
                                             agg_col="agg_off")
        sd, idc = calibrate_projected_ratings(A, ts, target_season=s, target="def_rating",
                                              agg_col="agg_def_used")
        sub = A[A.season_start == s].copy()
        sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub.agg_def_used + idc
        cal.append(sub)
    cal = pd.concat(cal, ignore_index=True)
    actual2 = ts.assign(net_rating_dev=ts.net_rating
                        - ts.groupby("season_start").net_rating.transform("mean"))
    return cal, actual2


def _training_states(gl, cal, ts, n, before_season):
    """Per-season (state, prior, remaining schedule, params) for all completed seasons < before."""
    out = []
    for s in range(2016, before_season):
        if s in SHORT or (s - 1) in SHORT:
            continue
        gs = gl[gl.SEASON_START == s].copy()
        if gs.empty or (cal.season_start == s).sum() == 0:
            continue
        state, d = through_split(gs, n)
        rem = extract_schedule(gs[gs.GAME_DATE >= d], s)
        if rem.empty:
            continue
        hca, msd = estimate_game_params(gl, before_season=s)
        pc = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
        prior = dict(zip(pc.team_id, pc.pred_net_rating_dev))
        out.append({"state": state, "prior": prior, "rem_sched": rem, "hca": hca, "msd": msd})
    return out


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, default=2024, help="season_start (2024 = 2024-25)")
    ap.add_argument("--games", type=int, default=25, help="games-played checkpoint N")
    args = ap.parse_args()
    S, N = args.season, args.games

    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    gl = pd.read_parquet(PROC / "game_log.parquet")
    gl["GAME_DATE"] = pd.to_datetime(gl["GAME_DATE"])
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ages = pd.DataFrame({"player_id": pa["PLAYER_ID"].astype("int64"),
                         "season_start": pa["SEASON_START"].astype(int),
                         "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()

    cal, actual2 = _prior_pipeline(imp, pts, pgl, ts, ages, rosters, last=min(S, 2025))

    # In-season state for the target season.
    gs = gl[gl.SEASON_START == S].copy()
    if gs.empty:
        raise SystemExit(f"no game_log rows for season_start={S}; pull the season first "
                         f"(scripts/fetch_all.py) before an in-season run.")
    state, d = through_split(gs, N)

    # Preseason prior for the target season.
    if S <= 2025 and (cal.season_start == S).sum() > 0:
        sub = apply_carryover(cal[cal.season_start == S].copy(), cal, ts, target_season=S)
        prior = dict(zip(sub.team_id, sub.pred_net_rating_dev))
    else:  # upcoming season: use the live preseason bundle's ratings
        live = json.loads((PROC / "projections_current.json").read_text())
        abbr2id = {t["abbr"]: t["id"] for t in live["teams"]}
        prior = {abbr2id[t["abbr"]]: t["rating"] for t in live["teams"]}

    # Remaining schedule: exact from game_log if the season has post-split games, else prev-season.
    rem = extract_schedule(gs[gs.GAME_DATE >= d], S)
    remaining_source = "gamelog"
    if rem.empty:
        rem = extract_schedule(gl, S - 1)
        remaining_source = "prev-season-standin"

    # Fit w walk-forward on all completed seasons before S at this N; simulate the remaining games.
    w = fit_w(_training_states(gl, cal, ts, N, before_season=S))
    rmap = blend_rating(state, prior, w)
    teams = list(state.team_id)
    hca, msd = estimate_game_params(gl, before_season=S)
    sigma = fit_rating_sigma(cal, actual2[["team_id", "season_start", "net_rating_dev"]],
                             before_season=min(S, 2025) + 1)
    ratings = pd.DataFrame({"team_id": teams,
                            "pred_net_rating_dev": [rmap[t] for t in teams]})
    sim, wins = simulate_season(ratings, rem, hca=hca, margin_sd=msd, sigma_rating=sigma,
                                n_sims=N_SIMS, seed=7)
    order = {t: i for i, t in enumerate(sim.team_id)}

    banked = dict(zip(state.team_id, state.banked_wins))
    obs = dict(zip(state.team_id, state.obs_rating))
    id2abbr = gs.groupby("TEAM_ID")["TEAM_ABBREVIATION"].first().to_dict()
    out_teams = []
    for t in teams:
        col = wins[:, order[t]].astype(float)
        rem_mean = float(col.mean())
        out_teams.append({
            "abbr": id2abbr.get(t, str(t)), "id": int(t),
            "banked_wins": banked[t],
            "proj_remaining_wins": round(rem_mean, 1),
            "wins": round(banked[t] + rem_mean, 1),
            "p10": round(banked[t] + float(np.percentile(col, 10)), 1),
            "p90": round(banked[t] + float(np.percentile(col, 90)), 1),
            "obs_rating": round(obs[t], 2),
            "prior_rating": round(float(prior.get(t, 0.0)), 2),
            "rating": round(rmap[t], 2),
        })
    out_teams.sort(key=lambda x: -x["wins"])
    bundle = {
        "meta": {
            "season": f"{S}-{str(S + 1)[-2:]}", "model": "inseason",
            "games_played": N, "split_date": str(d)[:10], "w": round(w, 3),
            "remaining_source": remaining_source, "snapshot_date": date.today().isoformat(),
        },
        "teams": out_teams,
    }
    path = PROC / "projection_inseason.json"
    path.write_text(json.dumps(bundle, indent=1))

    print(f"{bundle['meta']['season']} REST-OF-SEASON projection @ {N} games "
          f"(split {bundle['meta']['split_date']}, w={w:.2f}, remaining={remaining_source})\n")
    print(f"{'team':<5}{'banked':>7}{'+rem':>7}{'=full':>7}{'  obs':>7}{' prior':>7}")
    for t in out_teams:
        print(f"{t['abbr']:<5}{t['banked_wins']:>7}{t['proj_remaining_wins']:>7.1f}"
              f"{t['wins']:>7.1f}{t['obs_rating']:>7.1f}{t['prior_rating']:>7.1f}")
    print(f"\nwrote {path}  (sum full wins = {sum(t['wins'] for t in out_teams):.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
