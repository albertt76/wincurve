"""Pre-check (defensive roadmap item #2): does MULTI-SEASON DECAYED RAPM out-predict
single-season RAPM (and the box metric) on next-season team defense? Verdict: NO -- SKIP.

Our RAPM is single-season, which is noisy and needs heavy box-shrinkage; real RAPM systems use
multi-year decayed windows for stability. This pools play-by-play stints from {T-1, T-2, T-3} with
decay weights and fits ONE ridge RAPM, then runs the non-circular predictive test from
scripts/rapm_predict.py: predict each team's season-T defensive rating from its roster's PRIOR
metric, valued minute-weighted. Two most recent transitions (2023->24, 2024->25).

Result (2026-08-02): multi-season is <= single-season in ALL 6 variant-transitions, and the box
metric now BEATS every RAPM variant.

    box-informed, decay 1.0/0.7/0.5:  multi 0.583   single 0.591   box 0.611
    pure,          decay 1.0/0.7/0.5:  multi 0.554   single 0.561   box 0.611
    pure,          decay 1.0/0.85/0.72: multi 0.548  single 0.561   box 0.611

Why it fails: at alpha=2000 single-season regulars already have ample possessions, so pooling
mostly injects staleness (T-2/T-3 value) rather than reducing variance. And after this cycle's box
fixes (position-relative rebounding + tracking + BPM position + RAPM-prior refit) the box metric is
the strongest predictor here -- consistent with pure-RAPM 7.83 > pure-box 7.77 at the win level.
So multi-season makes the RAPM signal slightly WORSE and cannot help the turnover blend. Not built.

Trap worth recording: per100 = 100/poss inside build_design, so decay must scale the ridge
SAMPLE WEIGHT (possession weight), never the poss column (which would change y).

    python scripts/precheck_multiseason_rapm.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import sparse  # noqa: E402
from sklearn.linear_model import Ridge  # noqa: E402

from nbaproj.bulk_pbp import segments_for_season  # noqa: E402
from nbaproj.rapm import build_design, fit_rapm  # noqa: E402
from nbaproj.teams import load_team_seasons  # noqa: E402

PROC = Path("data/processed")
ALPHA = 2000.0
DECAY = {0: 1.0, 1: 0.7, 2: 0.5}  # offset from target-1: T-1, T-2, T-3

imp = pd.read_parquet(PROC / "player_impact.parquet")
pts = pd.read_parquet(PROC / "player_team_seasons.parquet")
ts = load_team_seasons().copy()
ts["def_dev"] = -(ts["def_rating"] - ts.groupby("season_start")["def_rating"].transform("mean"))


def fit_multiseason(prev: int, alpha: float, prior) -> pd.DataFrame:
    """One box-informed RAPM over {prev, prev-1, prev-2}, decay-weighting the possession weight."""
    per_season, all_players = [], set()
    for off in (0, 1, 2):
        X, y, w, players = build_design(segments_for_season(prev - off))
        per_season.append((X, y, w, players, DECAY[off]))
        all_players.update(players)
    players = sorted(all_players)
    idx = {p: j for j, p in enumerate(players)}
    n = len(players)
    rows, ys_all, ws_all = [], [], []
    for X, y, w, pl, d in per_season:
        k = len(pl)
        colmap = np.empty(2 * k, dtype=int)
        for j, p in enumerate(pl):
            colmap[j] = idx[p]              # offense column
            colmap[k + j] = n + idx[p]     # defense column
        Xc = X.tocoo()
        Xg = sparse.csr_matrix((Xc.data, (Xc.row, colmap[Xc.col])), shape=(X.shape[0], 2 * n))
        rows.append(Xg)
        ys_all.append(y)
        ws_all.append(w * d)               # decay scales the sample weight, NOT the poss column
    Xall = sparse.vstack(rows).tocsr()
    yall = np.concatenate(ys_all)
    wall = np.concatenate(ws_all)

    target = yall - np.average(yall, weights=wall)
    beta_prior = np.zeros(2 * n)
    if prior is not None and not prior.empty:
        pmap = prior.set_index("player_id")
        for j, p in enumerate(players):
            if p in pmap.index:
                r = pmap.loc[p]
                beta_prior[j] = 0.0 if pd.isna(r.get("off_prior")) else float(r["off_prior"])
                beta_prior[n + j] = 0.0 if pd.isna(r.get("def_prior")) else -float(r["def_prior"])
        target = target - Xall @ beta_prior
    model = Ridge(alpha=alpha, fit_intercept=False).fit(Xall, target, sample_weight=wall)
    beta = model.coef_ + beta_prior
    return pd.DataFrame({"player_id": [int(p) for p in players],
                         "off_rapm": beta[:n], "def_rapm": -beta[n:]})


def team_predict(roster, vals, col):
    d = roster.merge(vals, on="player_id", how="left").dropna(subset=[col])
    g = d.groupby("team_id").apply(lambda x: np.average(x[col], weights=x["minutes"]),
                                   include_groups=False)
    return g.rename("pred").reset_index()


def single_season_rapm(s, prior):
    f = PROC / f"rapm_{s}_a{int(ALPHA)}.parquet"
    if f.exists():
        return pd.read_parquet(f)[["player_id", "def_rapm"]]
    return fit_rapm(segments_for_season(s), alpha=ALPHA, prior=prior)[["player_id", "def_rapm"]]


def _run(prior_mode: str):
    print(f"\n=== {prior_mode}  decay {DECAY} ===")
    print(f"{'trans':>12} {'multi r':>8} {'single r':>9} {'box r':>7}")
    res = []
    for target in (2024, 2025):
        prev = target - 1
        prior = None
        if prior_mode == "box-informed":
            prior = imp[imp.season_start == prev][
                ["player_id", "off_impact", "def_impact"]].rename(
                columns={"off_impact": "off_prior", "def_impact": "def_prior"})
        multi = fit_multiseason(prev, ALPHA, prior)[["player_id", "def_rapm"]].rename(
            columns={"def_rapm": "def_multi"})
        single = (single_season_rapm(prev, prior) if prior_mode == "box-informed"
                  else fit_rapm(segments_for_season(prev), alpha=ALPHA,
                                prior=None)[["player_id", "def_rapm"]])
        box_prev = imp[imp.season_start == prev][["player_id", "def_impact"]]
        roster = pts[pts.season_start == target].groupby(
            ["team_id", "player_id"], as_index=False)["minutes"].sum()
        act = ts[ts.season_start == target][["team_id", "def_dev"]]
        r_m = team_predict(roster, multi, "def_multi").merge(act, on="team_id").dropna()
        r_s = team_predict(roster, single, "def_rapm").merge(act, on="team_id").dropna()
        r_b = team_predict(roster, box_prev, "def_impact").merge(act, on="team_id").dropna()
        rm, rs, rb = (r_m["pred"].corr(r_m["def_dev"]), r_s["pred"].corr(r_s["def_dev"]),
                      r_b["pred"].corr(r_b["def_dev"]))
        res.append((rm, rs, rb))
        print(f"  {prev}->{target}   {rm:>7.3f}  {rs:>8.3f}  {rb:>6.3f}")
    print(f"  {'MEAN':>10}  {np.mean([x[0] for x in res]):>7.3f}  "
          f"{np.mean([x[1] for x in res]):>8.3f}  {np.mean([x[2] for x in res]):>6.3f}")


def main() -> int:
    _run("box-informed")
    DECAY.clear(); DECAY.update({0: 1.0, 1: 0.7, 2: 0.5})
    _run("pure")
    DECAY.clear(); DECAY.update({0: 1.0, 1: 0.85, 2: 0.72})
    _run("pure")
    print("\nVERDICT: multi-season decayed RAPM <= single-season in all variants, and box beats "
          "all\nRAPM variants -> SKIP (do not build). See CLAUDE.md negative-results table.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
