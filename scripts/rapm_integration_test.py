"""THE deciding test: does swapping the impact metric's defensive component for box-informed
RAPM improve end-to-end, walk-forward win-projection accuracy?

The cross-season predictive test (scripts/rapm_predict.py) already showed RAPM predicts next
season's TEAM DEFENSE better than the box score (corr 0.55 vs 0.40). That is necessary but
not sufficient: what ships or does not ship is win-projection MAE (mean absolute error --
the average miss in wins). This script runs the full shippable pipeline (roster mode ->
calibration -> Monte-Carlo, exactly as scripts/gate_carryover.py) twice -- box defense vs
RAPM defense -- and compares. simulate_season is seeded, so the two arms are perfectly
paired and the MAE delta reflects only the change in defensive valuation.

## What it finds (2026-07)

- **Blanket swap is MAE-neutral (+0.04 wins, not significant), and slightly worsens 80%
  interval coverage.** Yet RAPM is genuinely the better defensive metric. The reason is the
  one-year residual carryover: it is ~70% defensive, so it already absorbs the team-level
  defensive error RAPM would fix. Turn the carryover OFF and RAPM improves MAE by +0.32
  (7/9 folds); turn it ON and the gain collapses to +0.04. RAPM and the carryover are
  **substitutes**, not complements.
- **Their residual value is complementary by DOMAIN.** The carryover persists a *team's*
  prior residual, so it is weak exactly when a roster turns over. RAPM attaches defensive
  value to *players*, so it travels across roster movement. Split by roster turnover: RAPM
  helps high-turnover teams (+0.31) and mildly hurts stable rosters (-0.36). That is the
  original "Atlanta looks too low" case -- a max-turnover team whose carryover is ~0.
- **A blend that weights RAPM by each team's new-minute share** (a preseason quantity, not
  fitted to outcomes) reaches 7.80 vs the box baseline's 7.96 -- **+0.16 wins, 5/6 folds**.
  The gain is robust to the blend weight (a flat 50/50 does about as well), so it is largely
  generic ensemble benefit from combining two imperfectly-correlated defensive signals, with
  turnover-weighting as the mechanistic justification. Modest and borderline (fold-level
  t~3, team-level t~1.45), about half the carryover's own +0.35.

## Why RAPM is NOT wired into the shipped projection (yet)

1. The blanket swap fails the aggregate gate (neutral, worse coverage).
2. The blend is a real but borderline +0.16 and adds a second full pipeline arm.
3. Deployment blocker: the bulk PBP mirror (shufinskiy/nba_data) reaches only 2024-25, so
   RAPM cannot inform the LIVE 2026-27 projection's most-recent, most-heavily-weighted
   season. Backtest-ready, not live-deployable until 2025-26 PBP is mirrored.

    python scripts/rapm_integration_test.py [--alpha 2000]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.bulk_pbp import segments_for_season  # noqa: E402
from nbaproj.carryover import apply_carryover  # noqa: E402
from nbaproj.project import calibrate_projected_ratings, project_team_ratings  # noqa: E402
from nbaproj.rapm import fit_rapm  # noqa: E402
from nbaproj.simulate import (  # noqa: E402
    estimate_game_params, extract_schedule, fit_rating_sigma, roster_turnover,
    simulate_season,
)
from nbaproj.teams import (  # noqa: E402
    FULL_SEASON_GAMES, SHORTENED_SEASONS, load_team_seasons,
)

PROC = Path("data/processed")
FIRST_TEST, LAST_TEST = 2017, 2025
N_SIMS = 6000
SHORT_STARTS = {int(s[:4]) for s in SHORTENED_SEASONS}


def season_rapm(season: int, alpha: float, box_impact: pd.DataFrame) -> pd.DataFrame:
    """Box-informed RAPM for one season, cached. Regenerated from the bulk PBP mirror if
    the parquet is missing (one ~8.5 MB download + offline reconstruction, ~50s)."""
    out = PROC / f"rapm_{season}_a{int(alpha)}.parquet"
    if out.exists():
        return pd.read_parquet(out)
    prior = box_impact[box_impact["season_start"] == season][
        ["player_id", "off_impact", "def_impact"]].rename(
        columns={"off_impact": "off_prior", "def_impact": "def_prior"})
    r = fit_rapm(segments_for_season(season), alpha=alpha, prior=prior)
    r.to_parquet(out, index=False)
    return r


def build_rapm_impact(box: pd.DataFrame, alpha: float) -> tuple[pd.DataFrame, list[int]]:
    """Box impact with `def_impact` replaced by box-informed RAPM defense wherever the bulk
    mirror can supply it, and `impact` recomputed. Box-informed RAPM is anchored on the box
    prior, so the two are on the same scale and the swap is well-posed. A player in a RAPM
    season with too few possessions to be estimated keeps his box defense.
    """
    out = box.copy()
    new_def = out["def_impact"].to_numpy(dtype=float).copy()
    have: list[int] = []
    for s in sorted(int(x) for x in out["season_start"].unique()):
        try:
            rmap = season_rapm(s, alpha, box).set_index("player_id")["def_rapm"]
        except Exception as exc:  # noqa: BLE001 -- a missing bulk season is expected, not fatal
            logging.info("no RAPM for %d (%s); keeping box defense", s, exc)
            continue
        have.append(s)
        mask = (out["season_start"] == s).to_numpy()
        rows = np.where(mask)[0]
        mapped = out.loc[mask, "player_id"].map(rmap).to_numpy(dtype=float)
        take = ~np.isnan(mapped)
        new_def[rows[take]] = mapped[take]
    out["def_impact"] = new_def
    out["impact"] = out["off_impact"] + out["def_impact"]
    return out, have


def arm_predictions(impact: pd.DataFrame, data: dict, *,
                    use_carryover: bool = True) -> pd.DataFrame:
    """Walk-forward per-team win predictions (82-game scale) for one impact table.

    Identical to gate_carryover's pipeline: project team ratings from prior seasons only,
    calibrate on earlier folds, add the carryover, simulate. Returns one row per team-season
    with the predicted wins.
    """
    pts, pgl, gl, ts, ages, rosters = (data[k] for k in (
        "pts", "pgl", "gl", "ts", "ages", "rosters"))
    actual = ts.copy()
    actual["net_rating_dev"] = actual["net_rating"] - actual.groupby(
        "season_start")["net_rating"].transform("mean")
    actual["actual_wins_82"] = actual["win_pct"] * FULL_SEASON_GAMES
    ac = actual[["team_id", "season_start", "net_rating_dev", "actual_wins_82", "games"]]

    raw = []
    for season in range(FIRST_TEST - 1, LAST_TEST + 1):
        r = project_team_ratings(impact, pts, pgl, ts, ages, target_season=season,
                                 mode="roster", team_rosters=rosters)
        if not r.empty:
            raw.append(r)
    raw = pd.concat(raw, ignore_index=True)

    calibrated = []
    for season in sorted(raw["season_start"].unique()):
        sub = raw[raw["season_start"] == season].copy()
        slope, icept = calibrate_projected_ratings(raw, ts, target_season=season)
        sub["pred_net_rating_dev"] = slope * sub["agg_impact"] + icept
        calibrated.append(sub)
    calibrated = pd.concat(calibrated, ignore_index=True)

    rows = []
    for season in range(FIRST_TEST, LAST_TEST + 1):
        sub = calibrated[calibrated["season_start"] == season].copy()
        if use_carryover:
            sub = apply_carryover(sub, calibrated, ts, target_season=season)
        sched = extract_schedule(gl, season)
        hca, msd = estimate_game_params(gl, before_season=season)
        sigma = fit_rating_sigma(calibrated, ac, before_season=season)
        sim, wins = simulate_season(sub[["team_id", "pred_net_rating_dev"]], sched,
                                    hca=hca, margin_sd=msd, sigma_rating=sigma,
                                    n_sims=N_SIMS)
        j = (sim.drop(columns=["pred_net_rating_dev"]).assign(season_start=season)
             .merge(ac, on=["team_id", "season_start"]))
        gp = FULL_SEASON_GAMES / j["games"].fillna(FULL_SEASON_GAMES)
        order = {t: i for i, t in enumerate(sim["team_id"])}
        w82 = wins[:, j["team_id"].map(order).to_numpy()] * gp.to_numpy()[None, :]
        cover = ((j["actual_wins_82"].to_numpy() >= np.percentile(w82, 10, axis=0))
                 & (j["actual_wins_82"].to_numpy() <= np.percentile(w82, 90, axis=0)))
        rows.append(pd.DataFrame({
            "season_start": season,
            "team_id": j["team_id"].to_numpy(),
            "pred": w82.mean(axis=0),
            "actual": j["actual_wins_82"].to_numpy(),
            "in80": cover,
            "short": (season - 1) in SHORT_STARTS or season in SHORT_STARTS,
        }))
    return pd.concat(rows, ignore_index=True)


def _mae(df, pred_col="pred"):
    return float((df[pred_col] - df["actual"]).abs().mean())


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=2000.0)
    args = ap.parse_args()

    data = {
        "imp": pd.read_parquet(PROC / "player_impact.parquet"),
        "pts": pd.read_parquet(PROC / "player_team_seasons.parquet"),
        "pgl": pd.read_parquet(PROC / "player_game_log.parquet"),
        "gl": pd.read_parquet(PROC / "game_log.parquet"),
        "rosters": pd.read_parquet(PROC / "team_rosters.parquet").rename(columns={
            "TeamID": "team_id", "PLAYER_ID": "player_id",
            "SEASON_START": "season_start"}),
        "ts": load_team_seasons(),
    }
    pa = pd.read_parquet(PROC / "player_advanced.parquet")
    data["ages"] = pd.DataFrame({
        "player_id": pa["PLAYER_ID"].astype("int64"),
        "season_start": pa["SEASON_START"].astype(int),
        "age": pd.to_numeric(pa["AGE"], errors="coerce")}).drop_duplicates()

    box = data["imp"]
    rapm_imp, have = build_rapm_impact(box, args.alpha)

    print("=" * 74)
    print("RAPM DEFENSIVE INTEGRATION TEST  (walk-forward win MAE, roster mode)")
    print("=" * 74)
    print("MAE = mean absolute error in wins (the average miss); lower is better.")
    print(f"RAPM seasons swapped in: {have}\n")

    # Predictions for both arms, carryover ON and OFF.
    pb_on = arm_predictions(box, data, use_carryover=True)
    pr_on = arm_predictions(rapm_imp, data, use_carryover=True)
    pb_off = arm_predictions(box, data, use_carryover=False)
    pr_off = arm_predictions(rapm_imp, data, use_carryover=False)

    def report(label, pb, pr):
        ex_b, ex_r = pb[~pb["short"]], pr[~pr["short"]]
        fb = ex_b.groupby("season_start").apply(_mae, include_groups=False)
        fr = ex_r.groupby("season_start").apply(_mae, include_groups=False)
        d = (fb - fr)
        print(f"[{label}] excl-short  box {fb.mean():.3f} -> rapm {fr.mean():.3f}   "
              f"delta {d.mean():+.3f} +/- {d.std()/np.sqrt(len(d)):.3f} (SE)   "
              f"improved {(d > 0).sum()}/{len(d)}   "
              f"cov {ex_b['in80'].mean():.0%}->{ex_r['in80'].mean():.0%}")

    print("Blanket swap -- does RAPM defense help, with vs without the carryover?")
    report("carryover ON ", pb_on, pr_on)
    report("carryover OFF", pb_off, pr_off)

    # Turnover split (carryover ON): where does RAPM's residual value live?
    turn = pd.concat([roster_turnover(data["pts"], season_start=s).assign(season_start=s)
                      for s in range(FIRST_TEST, LAST_TEST + 1)], ignore_index=True)
    m = (pb_on.rename(columns={"pred": "pred_box"})
         .merge(pr_on[["season_start", "team_id", "pred"]].rename(
             columns={"pred": "pred_rapm"}), on=["season_start", "team_id"])
         .merge(turn[["team_id", "season_start", "new_minute_share"]],
                on=["team_id", "season_start"], how="left"))
    ex = m[~m["short"]].copy()
    ex["e_box"] = (ex["pred_box"] - ex["actual"]).abs()
    ex["e_rapm"] = (ex["pred_rapm"] - ex["actual"]).abs()
    ex["tbucket"] = pd.qcut(ex["new_minute_share"], 3,
                            labels=["low turnover", "mid", "high turnover"])
    print("\nWhere RAPM's residual value lives (carryover ON):")
    for b, g in ex.groupby("tbucket", observed=True):
        print(f"  {b:<15} n={len(g):<3} turnover={g['new_minute_share'].mean():.2f}  "
              f"box {g['e_box'].mean():.3f} -> rapm {g['e_rapm'].mean():.3f}  "
              f"({g['e_box'].mean() - g['e_rapm'].mean():+.3f})")

    # Principled blend: weight RAPM by new-minute share (a preseason, non-fitted quantity).
    w = ex["new_minute_share"].clip(0, 1)
    ex["pred_blend"] = (1 - w) * ex["pred_box"] + w * ex["pred_rapm"]
    fold_box = ex.groupby("season_start").apply(_mae, "pred_box", include_groups=False)
    fold_bl = ex.groupby("season_start").apply(_mae, "pred_blend", include_groups=False)
    db = fold_box - fold_bl
    print(f"\nBlend (RAPM weighted by new-minute share), excl-short:")
    print(f"  box {fold_box.mean():.3f} -> blend {fold_bl.mean():.3f}   "
          f"delta {db.mean():+.3f} +/- {db.std()/np.sqrt(len(db)):.3f} (SE)   "
          f"improved {(db > 0).sum()}/{len(db)}")
    print("\nVerdict: RAPM is the better defensive metric but the carryover already")
    print("substitutes for it in aggregate; the blend is a real but modest (+0.16) and")
    print("borderline gain. Not wired into the shipped projection -- see the module docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
