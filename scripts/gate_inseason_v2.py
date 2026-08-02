"""Gate v2 of the in-season model: add a PER-PLAYER in-season talent update to v1's team-SRS blend.

v1 (shipped) shrinks each team's schedule-adjusted TEAM SRS toward its preseason projection:
    rest_rating = w·team_SRS + (1−w)·preseason_prior
v2 adds a second in-season signal, a PLAYER-UPDATED team rating: each player's this-season box
production through N games (scored with build_impact's own fitted coefficients) is blended into his
preseason-projected impact by sample seen (k = poss/(poss+K_stab)), then the CURRENT roster is
re-aggregated by each player's through-N minutes. Unlike the team SRS, this localizes performance to
players, so it can price minute redistribution and mid-season roster changes. Three-way blend:
    rest_rating = a·team_SRS + b·player_updated + (1−a−b)·preseason_prior     (a,b≥0, a+b≤1)
(a,b) fit walk-forward by the same deterministic expected-wins objective v1's w uses; v1 is the b=0
special case, so v2 can only win.

In-season box impact refreshes only the fast-stabilizing box countables; on/off (def_rating_rel) and
rim/hustle/RAPM tracking are held at their preseason value (not computable per-player through-N), so
the arm is offense-heavy and compresses stars' defense toward zero — which is why b should be small.

Honest expectation (from scoping): likely FLAT in aggregate (b→0), because the pre-deadline through-N
team SRS already saturates rest-of-season signal on a stable roster; v2's genuine value is the ~10%
of team-seasons with real roster churn, where the SRS is stale. So this gate reports BOTH the
aggregate and a CHURN-SUBGROUP MAE (teams with high offseason new-minute share) — the honest place v2
can win. Ship only if v2 clears the aggregate, or the turnover-conditional arm clears the subgroup;
else keep the machinery as a per-team diagnostic (player_updated vs SRS disagreement).

    python scripts/gate_inseason_v2.py
"""
from __future__ import annotations

import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.aging import (  # noqa: E402
    aging_curves, build_transitions, project_next_season, replacement_level)
from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.impact import (  # noqa: E402
    OFFENSE_FEATURES, DEFENSE_FEATURES, TRACKING_DEFENSE_FEATURES, SHOT_DEFENSE_FEATURES,
    POSITION_RELATIVE_FEATURES, build_team_features, _league_centered_targets,
    calibrate, apply_coefs, FLOOR_SPOTS)
from nbaproj.inseason import expected_wins, split_date, through_split  # noqa: E402
from nbaproj.project import calibrate_projected_ratings  # noqa: E402
from nbaproj.rapm import build_rapm_impact  # noqa: E402
from nbaproj.rapm_blend import backtest_aggregates, calibrate_blend  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, roster_turnover, simulate_season)
from nbaproj.teams import FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons  # noqa: E402

PROC = Path("data/processed")
FIRST, LAST, N_SIMS = 2017, 2025, 5000
SPLITS = (25, 50)
SHORT = {int(s[:4]) for s in SHORTENED_SEASONS}
MINUTES_PER_GAME = 240.0
K_STAB = 800.0                 # possessions of in-season box before the update outweighs the prior
CHURN_THRESH = 0.15            # "high churn" subgroup: offseason new-minute share above this
AB_GRID = np.round(np.arange(0.0, 1.001, 0.1), 3)
COUNT = ["FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB", "REB", "AST", "STL",
         "BLK", "TOV", "PF", "PTS"]


def _load():
    imp = pd.read_parquet(PROC / "player_impact.parquet")
    pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
    pgl = pd.read_parquet(PROC / "player_game_log.parquet")
    pgl["GAME_DATE"] = pd.to_datetime(pgl["GAME_DATE"])
    gl = pd.read_parquet(PROC / "game_log.parquet")
    gl["GAME_DATE"] = pd.to_datetime(gl["GAME_DATE"])
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    ts = load_team_seasons()
    rosters = pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
        "TeamID": "team_id", "PLAYER_ID": "player_id", "SEASON_START": "season_start"})
    ages = pd.DataFrame({"player_id": pa["PLAYER_ID"].astype("int64"),
                         "season_start": pa["SEASON_START"].astype(int),
                         "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()
    return imp, pts, pgl, gl, pa, ts, rosters, ages


# --- preseason prior + per-player projection (walk-forward) -------------------

def build_prior(imp, pts, pgl, ts, ages, rosters):
    rapm_imp = build_rapm_impact(imp, PROC)
    A = backtest_aggregates(imp, rapm_imp, pts, pgl, ts, ages, rosters, range(2016, LAST + 1))
    cal = []
    blends = {}
    for s in sorted(A.season_start.unique()):
        so, io = calibrate_projected_ratings(A, ts, target_season=s, target="off_rating",
                                             agg_col="agg_off")
        sd, idc = calibrate_projected_ratings(A, ts, target_season=s, target="def_rating",
                                              agg_col="agg_def_used")
        blends[s] = (so, io, sd, idc)
        sub = A[A.season_start == s].copy()
        sub["pred_net_rating_dev"] = so * sub.agg_off + io + sd * sub.agg_def_used + idc
        cal.append(sub)
    cal = pd.concat(cal, ignore_index=True)
    actual2 = ts.assign(net_rating_dev=ts.net_rating
                        - ts.groupby("season_start").net_rating.transform("mean"))
    return cal, actual2, blends


def prior_for(cal, ts, s):
    sub = apply_carryover(cal[cal.season_start == s].copy(), cal, ts, target_season=s)
    return dict(zip(sub.team_id, sub.pred_net_rating_dev))


# --- per-player in-season box impact (reuses build_impact's own coefficients) -

def _fold_setup(imp, pts, ts):
    track = [c for c in TRACKING_DEFENSE_FEATURES + SHOT_DEFENSE_FEATURES if c in imp.columns]
    def_features = DEFENSE_FEATURES + track
    all_features = list(dict.fromkeys(OFFENSE_FEATURES + def_features))
    tf = build_team_features(pts, imp, all_features)
    for c in track:
        tf[f"{c}_z"] = tf[f"{c}_z"].fillna(0.0)
    targets = _league_centered_targets(ts)
    return track, def_features, all_features, tf, targets


def fold_coefs(tf, targets, def_features, s):
    trt, trg = tf[tf.season_start < s], targets[targets.season_start < s]
    oc, _ = calibrate(trt, trg, OFFENSE_FEATURES, "off_rating_dev")
    dc, _ = calibrate(trt, trg, def_features, "def_rating_dev")
    return oc, dc


def preseason_player_proj(imp, s):
    hist = imp[imp.season_start < s]
    curves = aging_curves(build_transitions(hist, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)
    po = project_next_season(hist, curves, target_season=s, skill="off_impact")
    pd_ = project_next_season(hist, curves, target_season=s, skill="def_impact")
    return (dict(zip(po.player_id, po.proj_off_impact)),
            dict(zip(pd_.player_id, pd_.proj_def_impact)))


def inseason_player_impact(imp, pgl, gl, s, n, off_c, def_c, def_features, all_features, track):
    """Through-N per-player box impact + current team + through-N mpg (point-in-time)."""
    tg = gl[gl.SEASON_START == s]
    d_N = split_date(tg, n)
    g = pgl[(pgl.SEASON_START == s) & (pgl.GAME_DATE < d_N)]
    if g.empty:
        return pd.DataFrame(), d_N
    a = g.groupby("PLAYER_ID", as_index=False).agg(
        MIN=("MIN", "sum"), games=("GAME_ID", "nunique"),
        **{c.lower(): (c, "sum") for c in COUNT}).rename(columns={"PLAYER_ID": "player_id"})
    a["poss"] = a.fga - a.oreb + a.tov + 0.44 * a.fta
    for c in ["fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "reb", "ast",
              "stl", "blk", "tov", "pf", "pts"]:
        a[f"{c}_p100"] = np.where(a.poss > 0, a[c] * 100.0 / a.poss, np.nan)
    tsa = a.fga + 0.44 * a.fta
    a["ts_pct"] = np.where(tsa > 0, a.pts / (2 * tsa), np.nan)
    a["fg3_rate"] = np.where(a.fga > 0, a.fg3a / a.fga, np.nan)
    a["minutes"] = a["MIN"]
    a["has_rates"] = a.minutes >= 250.0 * n / 82.0
    a = a.merge(imp[imp.season_start == s][["player_id", "pos_group"]], on="player_id", how="left")

    def zc(df, cols, by=None):
        out = df.copy()
        for col in cols:
            out[f"{col}_z"] = np.nan
        subs = [(None, out)] if by is None else list(out.groupby(by))
        for _, sub in subs:
            for col in cols:
                m = sub["has_rates"] & sub[col].notna()
                if m.sum() < 12:
                    continue
                w = sub.loc[m, "minutes"].to_numpy(float)
                v = sub.loc[m, col].to_numpy(float)
                mu = np.average(v, weights=w)
                sd = np.sqrt(np.average((v - mu) ** 2, weights=w))
                if sd > 0:
                    out.loc[sub.loc[m].index, f"{col}_z"] = (v - mu) / sd
        return out

    posrel = [c for c in POSITION_RELATIVE_FEATURES if c in all_features]
    league = [c for c in all_features if c not in posrel and c != "def_rating_rel" and c not in track]
    a = zc(a, league)
    a = zc(a, posrel, by="pos_group")
    a["def_rating_rel_z"] = 0.0
    for c in track:
        a[f"{c}_z"] = 0.0
    off_z = [f"{c}_z" for c in OFFENSE_FEATURES]
    box_def = [f"{c}_z" for c in DEFENSE_FEATURES if c != "def_rating_rel"]
    mo = a[off_z].notna().all(axis=1)
    md = a[box_def].notna().all(axis=1)
    a["inseason_off"] = np.nan
    a["inseason_def"] = np.nan
    a.loc[mo, "inseason_off"] = apply_coefs(a.loc[mo], off_c, OFFENSE_FEATURES) / FLOOR_SPOTS
    a.loc[md, "inseason_def"] = apply_coefs(a.loc[md], def_c, def_features) / FLOOR_SPOTS
    a["tn_mpg"] = a["MIN"] / a["games"].clip(lower=1)
    # current team = the team of the player's most recent pre-split game (handles mid-season moves)
    last = (g.sort_values("GAME_DATE").groupby("PLAYER_ID")["TEAM_ID"].last()
            .rename("team_id").reset_index().rename(columns={"PLAYER_ID": "player_id"}))
    a = a.merge(last, on="player_id", how="left")
    return a, d_N


def player_updated_rating(a, proj_off, proj_def, blend, rep_off, rep_def):
    """k-blend per player, aggregate the current roster by through-N minutes, apply preseason slopes."""
    so, io, sd, idc = blend
    a = a.copy()
    a["k"] = a["poss"] / (a["poss"] + K_STAB)
    a["u_off"] = np.where(a.inseason_off.notna(),
                          a.k * a.inseason_off + (1 - a.k) * a.player_id.map(proj_off).astype(float),
                          a.player_id.map(proj_off))
    a["u_def"] = np.where(a.inseason_def.notna(),
                          a.k * a.inseason_def + (1 - a.k) * a.player_id.map(proj_def).astype(float),
                          a.player_id.map(proj_def))
    out = {}
    budget = MINUTES_PER_GAME * FULL_SEASON_GAMES
    for tid, grp in a.dropna(subset=["team_id"]).groupby("team_id"):
        mins = (grp.tn_mpg * FULL_SEASON_GAMES).to_numpy(float)
        off_v = grp.u_off.to_numpy(float)
        def_v = grp.u_def.to_numpy(float)
        ok = ~(np.isnan(off_v) | np.isnan(def_v))
        used = mins.sum()
        leftover = max(budget - used, 0.0) + mins[~ok].sum()

        def _agg(v, rep):
            tot = mins[ok].sum()
            return ((np.sum(mins[ok] * v[ok]) + leftover * rep) / max(budget, used)
                    if tot > 0 else rep)
        agg_off, agg_def = _agg(off_v, rep_off), _agg(def_v, rep_def)
        out[int(tid)] = so * agg_off + io + sd * agg_def + idc
    return out


# --- gate --------------------------------------------------------------------

def fit_ab(train):
    best, best_mae = (0.5, 0.0), np.inf
    for a in AB_GRID:
        for b in AB_GRID:
            if a + b > 1.0 + 1e-9:
                continue
            errs = []
            for t in train:
                rating = {tid: a * t["srs"].get(tid, 0.0) + b * t["upd"].get(tid, 0.0)
                          + (1 - a - b) * t["prior"].get(tid, 0.0) for tid in t["teams"]}
                ew = expected_wins(rating, t["rem"], t["hca"], t["msd"])
                errs += [abs(ew[tid] - t["rem_wins"][tid]) for tid in t["teams"]]
            mae = float(np.mean(errs))
            if mae < best_mae:
                best_mae, best = mae, (a, b)
    return best


def _sim(rating_map, rem, hca, msd, sigma, seed, teams):
    r = pd.DataFrame({"team_id": teams, "pred_net_rating_dev": [rating_map[t] for t in teams]})
    sim, wins = simulate_season(r, rem, hca=hca, margin_sd=msd, sigma_rating=sigma,
                                n_sims=N_SIMS, seed=seed)
    order = {t: i for i, t in enumerate(sim.team_id)}
    return {t: wins[:, order[t]].mean() for t in teams}


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    imp, pts, pgl, gl, pa, ts, rosters, ages = _load()
    print("building preseason prior + fold setup...", flush=True)
    cal, actual2, blends = build_prior(imp, pts, pgl, ts, ages, rosters)
    track, def_features, all_features, tf, targets = _fold_setup(imp, pts, ts)
    rep_off = replacement_level(imp, "off_impact")
    rep_def = replacement_level(imp, "def_impact")

    for N in SPLITS:
        per = {}
        for s in range(2016, LAST + 1):
            gs = gl[gl.SEASON_START == s]
            if gs.empty:
                continue
            state, d = through_split(gs, N)
            rem = extract_schedule(gs[gs.GAME_DATE >= d], s)
            if rem.empty:
                continue
            oc, dc = fold_coefs(tf, targets, def_features, s)
            po, pdf = preseason_player_proj(imp, s)
            a, _ = inseason_player_impact(imp, pgl, gl, s, N, oc, dc, def_features,
                                          all_features, track)
            upd = player_updated_rating(a, po, pdf, blends[s], rep_off, rep_def)
            hca, msd = estimate_game_params(gl, before_season=s)
            sigma = fit_rating_sigma(cal, actual2[["team_id", "season_start", "net_rating_dev"]],
                                     before_season=s)
            churn = roster_turnover(pts, season_start=s, roster=rosters[rosters.season_start == s])
            per[s] = {
                "teams": list(state.team_id),
                "srs": dict(zip(state.team_id, state.obs_rating)),
                "prior": prior_for(cal, ts, s),
                "upd": upd,
                "rem": rem, "hca": hca, "msd": msd, "sigma": sigma,
                "rem_wins": dict(zip(state.team_id, state.rem_wins)),
                "churn": dict(zip(churn.team_id, churn.new_minute_share)),
            }

        rows = []
        for S in range(FIRST, LAST + 1):
            if S in SHORT or (S - 1) in SHORT or S not in per:
                continue
            train = [per[s] for s in per if s < S and s not in SHORT and (s - 1) not in SHORT]
            if not train:
                continue
            # v1: fit a with b=0 (SRS vs prior); v2: fit (a,b).
            a1, _ = fit_ab([{**t, "upd": {k: 0.0 for k in t["upd"]}} for t in train])
            a2, b2 = fit_ab(train)
            st = per[S]
            teams = st["teams"]
            def rmap(a, b):
                return {t: a * st["srs"].get(t, 0.0) + b * st["upd"].get(t, 0.0)
                        + (1 - a - b) * st["prior"].get(t, 0.0) for t in teams}
            v1 = _sim(rmap(a1, 0.0), st["rem"], st["hca"], st["msd"], st["sigma"], 1000 + S, teams)
            v2 = _sim(rmap(a2, b2), st["rem"], st["hca"], st["msd"], st["sigma"], 1000 + S, teams)
            aw = st["rem_wins"]
            hi = {t for t in teams if st["churn"].get(t, 0.0) > CHURN_THRESH}
            rows.append({
                "season": S, "a1": a1, "a2": a2, "b2": b2, "n_hi": len(hi),
                "v1_mae": np.mean([abs(v1[t] - aw[t]) for t in teams]),
                "v2_mae": np.mean([abs(v2[t] - aw[t]) for t in teams]),
                "v1_hi": np.mean([abs(v1[t] - aw[t]) for t in hi]) if hi else np.nan,
                "v2_hi": np.mean([abs(v2[t] - aw[t]) for t in hi]) if hi else np.nan,
            })

        df = pd.DataFrame(rows)
        print(f"\n===== N = {N} games  ({len(df)} folds exShort) =====")
        print(df.round(3).to_string(index=False))
        dd = df.v1_mae - df.v2_mae
        print(f"\n  AGGREGATE rest-of-season MAE: v1 {df.v1_mae.mean():.3f}  v2 {df.v2_mae.mean():.3f}"
              f"   v2−v1 {dd.mean():+.3f} ±{dd.std()/np.sqrt(len(dd)):.3f}  {(dd>0).sum()}/{len(dd)}")
        hi = df.dropna(subset=["v1_hi", "v2_hi"])
        if len(hi):
            dh = hi.v1_hi - hi.v2_hi
            print(f"  CHURN SUBGROUP (offseason new-min share >{CHURN_THRESH}, "
                  f"{int(df.n_hi.mean())} teams/yr): v1 {hi.v1_hi.mean():.3f}  v2 {hi.v2_hi.mean():.3f}"
                  f"   v2−v1 {dh.mean():+.3f} ±{dh.std()/np.sqrt(len(dh)):.3f}  {(dh>0).sum()}/{len(dh)}")
        print(f"  mean fitted a(v1)={df.a1.mean():.2f}  a(v2)={df.a2.mean():.2f}  b(v2)={df.b2.mean():.2f}"
              f"   (b=weight on the player-updated arm; b→0 means v2 adds nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
