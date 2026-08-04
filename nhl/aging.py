"""Aging curves for xG-RAPM impact (Stage 3, step 3).

How does a skater's on-ice impact change with age? We need this to age a current-talent estimate
(``rapm.talent``) forward into a projection. Measured with the standard **delta method**, which is
robust to the fact that players have different baseline talent: for every skater who has a
single-season RAPM estimate in two consecutive seasons, take the *change* in his off/def/net from
season Y to Y+1 and attribute it to his age in Y. Averaging those changes across many players (TOI-
weighted, since a thin-sample estimate's change is noisier) traces the average year-over-year
*delta* by age; the cumulative sum of the deltas is the *level* curve, whose maximum is the peak age.

Modeled per skill (offense vs defense) because they peak at different ages (as in the NBA project).

**Survivorship caveat:** only players present in BOTH seasons contribute a delta, so a player who
declines out of the league is missing -- this biases the measured decline at older ages toward zero
(too optimistic). We do NOT gate on a large next-season role (only the shared 400-min RAPM floor),
which keeps the bias modest, but the oldest-age estimates are thin and should be read with care.
"""

from __future__ import annotations

import datetime as dt
import glob

import numpy as np
import pandas as pd

from . import ingest

# Reference date for age within a season: roughly opening night of season_start_year.
SEASON_AGE_MONTH, SEASON_AGE_DAY = 10, 1


def load_birthdates() -> pd.DataFrame:
    """player_id -> birthDate (from scripts/nhl_fetch_birthdates.py)."""
    df = pd.read_parquet(ingest.PROC / "player_birthdates.parquet")
    df = df.dropna(subset=["birthDate"]).copy()
    df["player_id"] = df["player_id"].astype(int)
    df["birth"] = pd.to_datetime(df["birthDate"])
    return df[["player_id", "birth"]]


def age_at(birth: pd.Series, season_start: pd.Series) -> pd.Series:
    """Fractional age as of ~opening night (Oct 1) of each row's season (vectorized)."""
    ref = pd.to_datetime(pd.DataFrame({
        "year": pd.Series(season_start).astype(int),
        "month": SEASON_AGE_MONTH, "day": SEASON_AGE_DAY}))
    return (ref - birth).dt.days / 365.25


def rapm_panel(alpha: float = 3000.0) -> pd.DataFrame:
    """Long panel of single-season xG-RAPM: one row per (player, season) from impact_<yr>_a<alpha>."""
    frames = []
    for f in sorted(glob.glob(str(ingest.PROC / f"impact_*_a{int(alpha)}.parquet"))):
        yr = int(f.split("impact_")[1].split("_")[0])
        d = pd.read_parquet(f)[["player_id", "off", "def", "net", "toi_min"]].copy()
        d["season_start"] = yr
        frames.append(d)
    panel = pd.concat(frames, ignore_index=True)
    panel["player_id"] = panel["player_id"].astype(int)
    return panel


def deltas(metric: str = "net", alpha: float = 3000.0) -> pd.DataFrame:
    """Per-player consecutive-season change in `metric`, tagged with age-in-Y and a weight.

    Weight = min(TOI_Y, TOI_{Y+1}) -- the change is only as reliable as the thinner of the two
    single-season estimates.
    """
    panel = rapm_panel(alpha)
    bd = load_birthdates()
    panel = panel.merge(bd, on="player_id", how="inner")
    panel["age"] = age_at(panel["birth"], panel["season_start"])

    a = panel.rename(columns={metric: "m0", "toi_min": "toi0", "age": "age0"})
    b = panel[["player_id", "season_start", metric, "toi_min"]].rename(
        columns={metric: "m1", "toi_min": "toi1", "season_start": "next"})
    a["next"] = a["season_start"] + 1
    pair = a.merge(b, on=["player_id", "next"])
    pair["d"] = pair["m1"] - pair["m0"]
    pair["w"] = np.minimum(pair["toi0"], pair["toi1"])
    return pair[["player_id", "season_start", "age0", "m0", "m1", "d", "w"]]


def curve(metric: str = "net", alpha: float = 3000.0,
          age_lo: int = 19, age_hi: int = 38, ref_age: int = 27) -> pd.DataFrame:
    """Aging curve for `metric`: TOI-weighted mean delta by integer age, and its integral (level).

    The level column is the cumulative sum of the per-age deltas, offset so ``ref_age`` = 0; its
    argmax is the peak age. Ages with too few player-pairs (<20) are dropped from the fit.
    """
    d = deltas(metric, alpha)
    d = d[(d["age0"] >= age_lo) & (d["age0"] < age_hi)]
    d["age_bin"] = d["age0"].astype(int)

    def wmean(g):
        return np.average(g["d"], weights=g["w"])

    g = d.groupby("age_bin")
    tbl = pd.DataFrame({
        "age": g.size().index,
        "n_pairs": g.size().values,
        "delta": g.apply(wmean, include_groups=False).values,   # avg change from `age` to `age`+1
    })
    tbl = tbl[tbl["n_pairs"] >= 20].reset_index(drop=True)
    # integrate deltas into a level curve; the delta at age A moves A -> A+1, so level[A+1]=level[A]+delta[A]
    level = np.concatenate([[0.0], np.cumsum(tbl["delta"].values)])
    tbl["level"] = level[:len(tbl)]
    tbl["level"] -= tbl.loc[tbl["age"] == ref_age, "level"].values[0] if (tbl["age"] == ref_age).any() \
        else tbl["level"].mean()
    return tbl


def smooth_curve(metric: str = "net", alpha: float = 3000.0, degree: int = 2, ref_age: int = 27):
    """Smoothed aging curve: weight-fit a degree-`degree` polynomial to the noisy per-age deltas
    (so a linear-in-age delta gives a parabolic level, degree 2 allows an accelerating decline),
    integrate to a level, and return (table, peak_age). Single-season RAPM deltas are noisy year to
    year, so the raw integer peak is unreliable; the smoothed level's argmax is the robust peak.
    """
    raw = curve(metric, alpha, ref_age=ref_age)
    coef = np.polyfit(raw["age"], raw["delta"], degree, w=np.sqrt(raw["n_pairs"]))
    ages = np.arange(int(raw["age"].min()), int(raw["age"].max()) + 1)
    sdelta = np.polyval(coef, ages)
    slevel = np.concatenate([[0.0], np.cumsum(sdelta)])[:len(ages)]
    tbl = pd.DataFrame({"age": ages, "sdelta": sdelta, "slevel": slevel})
    tbl["slevel"] -= tbl.loc[tbl["age"] == ref_age, "slevel"].values[0] if (tbl["age"] == ref_age).any() \
        else tbl["slevel"].mean()
    peak_age = int(tbl.loc[tbl["slevel"].idxmax(), "age"])
    return tbl, peak_age
