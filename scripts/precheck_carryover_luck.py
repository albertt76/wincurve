"""Pre-check: would luck-adjusting the carryover's residual (3P%/FT% replaced by that season's
league average, LEBRON-style) help predict next season's rating? Verdict: NO, decisively --
never reached the 5000-sim gate because this pre-check alone kills it.

Motivation (2026-07-31 deep dive on LEBRON's "luck adjustment"): the carryover is the one place
in wincurve where a raw realized quantity (last season's residual) is piped straight into next
season's prediction. The premise: team/opponent 3P% and FT% relative to league average contain a
volatile, largely-luck component, so persisting a residual computed from the RAW rating -- which
still has that luck baked in -- risks carrying forward noise rather than signal. Team-level
opponent 3P% allowed does have low season-to-season persistence (r=0.24 here, close to the
independently-cited 0.17), which sounds like it supports the idea.

Why it fails anyway. Two decisive checks, using only game_log.parquet (self-joined on GAME_ID for
each opponent's box score = what was allowed) and team_advanced.parquet (POSS) -- no new data:

1. Direct test: does a luck-adjusted (2PT makes + 3PA/FTA at league-average make-rate) season N-1
   rating correlate BETTER with season N's real rating than the raw N-1 rating?
       DEFENSE: r^2 raw 0.3055 -> luck-adjusted 0.2838   (WORSE, delta -0.022)
       OFFENSE: r^2 raw 0.3097 -> luck-adjusted 0.2256   (WORSE, delta -0.084)
   Luck-adjusting makes the predictor WORSE at predicting next season, on both sides of the ball.

2. Why: a joint regression of next-season defense on [luck-adjusted core, the stripped "luck"
   component] gives the stripped component a coefficient of +0.446, nearly as large as the core's
   +0.594 -- and the joint model's r^2 (0.307) barely beats the raw-only model's (0.3055). The
   "luck" component is NOT noise relative to next season's rating; it carries almost the same
   predictive weight as the "real skill" part LEBRON's premise says to keep.

Root cause, checked directly: OWN 3P%/FT% are far more persistent than commonly assumed for a
full-season sample -- own FT% year-to-year r^2 = 0.3185 (one of the most persistent box-score
rates there is), own 3P% r^2 = 0.1652. What IS mostly luck is what a team ALLOWS: opponent 3P%
against a team r^2 = 0.0584, opponent FT% against a team r^2 = 0.0130. A blanket "luck-adjust
3P/FT on both sides" throws out real, persistent OFFENSIVE shooting skill along with the volatile
DEFENSIVE-allowed noise, and the offensive loss outweighs the defensive gain.

This corroborates an independent public data point found in the same deep dive: PIPM, the only
metric in the Dunks & Threes retrodiction table built on luck-adjusted team ratings, finished 6th
of 10 -- behind plain BPM and RAPM. Do not re-attempt a blanket luck adjustment. A DEFENSE-only
luck adjustment (using only the allowed side, where the persistence really is low) was not tested
here and is the only variant left un-killed; it would still need to clear this same predictive-
correlation bar before any carryover code changes, since removing signal-bearing noise is exactly
the failure mode found above.

    python scripts/precheck_carryover_luck.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import numpy.linalg as la  # noqa: E402
import pandas as pd  # noqa: E402

PROC = Path("data/processed")
SHORT = {2011, 2019, 2020}


def _attach_opponent(gl: pd.DataFrame) -> pd.DataFrame:
    cols = ["GAME_ID", "TEAM_ID", "FGM", "FG3M", "FG3A", "FTM", "FTA"]
    g = gl[cols].copy()
    opp = g.rename(columns={c: f"opp_{c}" for c in cols if c not in ("GAME_ID", "TEAM_ID")}).rename(
        columns={"TEAM_ID": "opp_team_id"})
    m = g.merge(opp, on="GAME_ID")
    m = m[m["TEAM_ID"] != m["opp_team_id"]]
    m["SEASON_START"] = gl["SEASON_START"].to_numpy()
    m["team_id"] = m["TEAM_ID"]
    return m


def _rate_persistence(gl: pd.DataFrame, label: str, opp: bool) -> float:
    """Season-to-season r^2 of a make-rate (own or allowed) league-centered deviation."""
    m = _attach_opponent(gl)
    pre = "opp_" if opp else ""
    s = m.groupby(["team_id", "SEASON_START"], as_index=False).agg(
        m_=(f"{pre}FG3M", "sum"), a_=(f"{pre}FG3A", "sum"))
    s["pct"] = s.m_ / s.a_
    s["dev"] = s.pct - s.groupby("SEASON_START").pct.transform("mean")
    nxt = s[["team_id", "SEASON_START", "dev"]].copy()
    nxt["SEASON_START"] -= 1
    nxt = nxt.rename(columns={"dev": "next_dev"})
    p = s.merge(nxt, on=["team_id", "SEASON_START"])
    p = p[~p.SEASON_START.isin(SHORT) & ~(p.SEASON_START + 1).isin(SHORT)]
    r = np.corrcoef(p.dev, p.next_dev)[0, 1]
    print(f"  {label:<24} r={r:.3f}  r^2={r**2:.4f}  n={len(p)}")
    return r ** 2


def main() -> int:
    gl = pd.read_parquet(PROC / "game_log.parquet")
    ta = pd.read_parquet(PROC / "team_advanced.parquet")

    print("STEP 1 -- own vs opponent 3P%/FT% year-to-year persistence (r^2)\n")
    m = _attach_opponent(gl)

    def persistence(makecol, attcol, label, opp):
        pre = "opp_" if opp else ""
        s = m.groupby(["team_id", "SEASON_START"], as_index=False).agg(
            m_=(f"{pre}{makecol}", "sum"), a_=(f"{pre}{attcol}", "sum"))
        s["pct"] = s.m_ / s.a_
        s["dev"] = s.pct - s.groupby("SEASON_START").pct.transform("mean")
        nxt = s[["team_id", "SEASON_START", "dev"]].copy()
        nxt["SEASON_START"] -= 1
        nxt = nxt.rename(columns={"dev": "next_dev"})
        p = s.merge(nxt, on=["team_id", "SEASON_START"])
        p = p[~p.SEASON_START.isin(SHORT) & ~(p.SEASON_START + 1).isin(SHORT)]
        r = np.corrcoef(p.dev, p.next_dev)[0, 1]
        print(f"  {label:<24} r={r:.3f}  r^2={r**2:.4f}  n={len(p)}")

    persistence("FG3M", "FG3A", "own 3P%", False)
    persistence("FG3M", "FG3A", "opp 3P% (defense)", True)
    persistence("FTM", "FTA", "own FT%", False)
    persistence("FTM", "FTA", "opp FT% (defense)", True)

    print("\nSTEP 2 -- does luck-adjusting N-1 improve its correlation with N's real rating?\n")
    season = m.groupby(["team_id", "SEASON_START"], as_index=False).agg(
        fgm=("FGM", "sum"), fg3m=("FG3M", "sum"), fg3a=("FG3A", "sum"),
        ftm=("FTM", "sum"), fta=("FTA", "sum"),
        opp_fgm=("opp_FGM", "sum"), opp_fg3m=("opp_FG3M", "sum"), opp_fg3a=("opp_FG3A", "sum"),
        opp_ftm=("opp_FTM", "sum"), opp_fta=("opp_FTA", "sum"))
    lg = m.groupby("SEASON_START").agg(
        lg_fg3m=("FG3M", "sum"), lg_fg3a=("FG3A", "sum"), lg_ftm=("FTM", "sum"),
        lg_fta=("FTA", "sum"))
    lg["lg_fg3pct"] = lg.lg_fg3m / lg.lg_fg3a
    lg["lg_ftpct"] = lg.lg_ftm / lg.lg_fta
    season = season.merge(lg[["lg_fg3pct", "lg_ftpct"]], on="SEASON_START", how="left")

    season["pts_for_luckadj"] = ((season.fgm - season.fg3m) * 2
                                 + season.fg3a * season.lg_fg3pct * 3
                                 + season.fta * season.lg_ftpct)
    season["pts_against_luckadj"] = ((season.opp_fgm - season.opp_fg3m) * 2
                                     + season.opp_fg3a * season.lg_fg3pct * 3
                                     + season.opp_fta * season.lg_ftpct)

    ta2 = ta.rename(columns={"TEAM_ID": "team_id"})[
        ["team_id", "SEASON_START", "POSS", "OFF_RATING", "DEF_RATING"]]
    d = season.merge(ta2, on=["team_id", "SEASON_START"], how="inner")
    d["off_luckadj"] = d.pts_for_luckadj / d.POSS * 100
    d["def_luckadj"] = d.pts_against_luckadj / d.POSS * 100

    for c in ["OFF_RATING", "DEF_RATING", "off_luckadj", "def_luckadj"]:
        d[c + "_dev"] = d[c] - d.groupby("SEASON_START")[c].transform("mean")
    d["def_dev"] = -d["DEF_RATING_dev"]
    d["def_luckadj_dev"] = -d["def_luckadj_dev"]

    nxt = d[["team_id", "SEASON_START", "def_dev", "OFF_RATING_dev"]].copy()
    nxt["SEASON_START"] -= 1
    nxt = nxt.rename(columns={"def_dev": "next_def_dev", "OFF_RATING_dev": "next_off_dev"})
    pairs = d.merge(nxt, on=["team_id", "SEASON_START"], how="inner")
    pairs = pairs[~pairs.SEASON_START.isin(SHORT) & ~(pairs.SEASON_START + 1).isin(SHORT)]

    for label, raw_col, adj_col, target_col in [
        ("DEFENSE", "def_dev", "def_luckadj_dev", "next_def_dev"),
        ("OFFENSE", "OFF_RATING_dev", "off_luckadj_dev", "next_off_dev"),
    ]:
        r_raw = np.corrcoef(pairs[raw_col], pairs[target_col])[0, 1]
        r_adj = np.corrcoef(pairs[adj_col], pairs[target_col])[0, 1]
        print(f"  {label}: r^2 raw {r_raw**2:.4f}  ->  luck-adjusted {r_adj**2:.4f}   "
              f"delta {r_adj**2 - r_raw**2:+.4f}")

    print("\nSTEP 3 -- joint regression: is the stripped 'luck' component actually just noise?\n")
    pairs["luck_component"] = pairs.def_dev - pairs.def_luckadj_dev
    X = np.column_stack([pairs.def_luckadj_dev, pairs.luck_component, np.ones(len(pairs))])
    y = pairs.next_def_dev.to_numpy()
    beta, *_ = la.lstsq(X, y, rcond=None)
    pred = X @ beta
    r2_joint = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    r2_raw = np.corrcoef(pairs.def_dev, pairs.next_def_dev)[0, 1] ** 2
    print(f"  next_def_dev ~ {beta[0]:+.3f}*luckadj_core + {beta[1]:+.3f}*luck_component")
    print(f"  joint model r^2 = {r2_joint:.4f}   (raw-only r^2 was {r2_raw:.4f})")
    print(f"\nVERDICT: luck-adjusting the carryover residual does NOT help -- the 'luck' component")
    print(f"carries almost the same predictive weight as the core. Do not implement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
