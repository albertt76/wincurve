"""xG-RAPM: regularized adjusted plus-minus on expected goals.

RAPM (regularized adjusted plus-minus) is a ridge regression that credits each skater's
on-ice impact while controlling for the four teammates and five opponents sharing the ice.
We run it on **expected goals (xG)** rather than actual goals -- goals are far too sparse
in a low-scoring sport for a stint-level fit -- exactly as the NBA project runs RAPM on
play-by-play margins. This is the direct hockey analog.

Setup (even strength, 5v5 only): every stint contributes TWO observations, one per team's
offense. For "team A attacking", the response is A's xG-for per 60 minutes during the stint;
the design has a +1 in the OFFENSE column of each of A's five skaters and a +1 in the
DEFENSE column of each of B's five skaters, plus a home-ice term. Ridge then splits credit.

Output per skater: ``off`` (xG/60 a player adds on offense; higher is better) and ``def``
(xG/60 he suppresses on defense; defined so higher is better, i.e. the negative of the
raw "xG allowed" coefficient), and ``net = off + def``. All are per-60 rate impacts.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.linear_model import Ridge

from . import ingest
from .shifts import game_stints

DEFAULT_ALPHA = 3000.0  # ridge strength; RAPM needs heavy shrinkage on ~900 skaters
DEFAULT_MIN_TOI = 24000  # 400 min of 5v5; below this a season's estimate is mostly noise

# MoneyPuck used dotted team codes for four teams through 2020 (empirically 2007-08..2019-20);
# team_reference uses the NHL tricodes. Without this normalization ~13% of pre-2021 5v5 shots
# fail the id join and get misattributed to the OTHER on-ice team -- and because New Jersey has
# the lowest team_id it is always "team0", so 100% of NJD's offense would be credited to its
# opponents. Fail loud (below) if a future code slips through.
MP_CODE_ALIASES = {"L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL"}


def build_stints(start_year: int) -> pd.DataFrame:
    """All 5v5 stints for a season (reconstructed from the cached shift charts)."""
    shifts = pd.read_parquet(ingest.PROC / f"shifts_{start_year}.parquet")
    goalies = set(pd.read_parquet(ingest.PROC / "moneypuck_goalies.parquet")
                  ["playerId"].dropna().astype(int))
    frames = [game_stints(g, goalies) for _, g in shifts.groupby("gameId", sort=False)]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True)


def attach_xg(stints: pd.DataFrame, start_year: int) -> pd.DataFrame:
    """Attribute each 5v5 shot's xG to its stint, giving xgf0 / xgf1 (xG for team0/team1)."""
    ref = pd.read_parquet(ingest.PROC / "team_reference.parquet")
    code2id = dict(zip(ref["tricode"], ref["team_id"]))

    sh = ingest.moneypuck_shots(start_year)
    sh = sh[(sh["isPlayoffGame"] == 0) & (sh["homeSkatersOnIce"] == 5)
            & (sh["awaySkatersOnIce"] == 5)].copy()
    # Normalize MoneyPuck's legacy dotted codes to NHL tricodes on every code column, THEN join
    # to team ids and fail loud on any leftover unmapped code (per the project's join discipline).
    for col in ("teamCode", "homeTeamCode", "awayTeamCode"):
        sh[col] = sh[col].replace(MP_CODE_ALIASES)
    sh["shoot_id"] = sh["teamCode"].map(code2id)
    unmapped = sorted(set(sh.loc[sh["shoot_id"].isna(), "teamCode"].dropna().unique()))
    if unmapped:
        raise RuntimeError(f"attach_xg {start_year}: MoneyPuck team codes not in team_reference "
                           f"{unmapped} -- extend nhl.rapm.MP_CODE_ALIASES")

    stints = stints.sort_values(["gid", "start"]).reset_index(drop=True)
    # Attribute each 5v5 shot to the stint containing its time, per game. Stints within a
    # game are non-overlapping and start-sorted, so searchsorted finds the candidate; a shot
    # landing in a gap (power play / penalty kill) fails the end-check and is dropped.
    xgf0 = np.zeros(len(stints))
    xgf1 = np.zeros(len(stints))
    stints_by_gid = {gid: g for gid, g in stints.groupby("gid")}
    for gid, sg in sh.groupby("gid"):
        st = stints_by_gid.get(gid)
        if st is None:
            continue
        starts, ends = st["start"].to_numpy(), st["end"].to_numpy()
        rowidx, team0 = st.index.to_numpy(), st["team0"].to_numpy()
        t = sg["time"].to_numpy()
        pos = np.searchsorted(starts, t, side="right") - 1
        ok = (pos >= 0) & (t < ends[np.clip(pos, 0, len(ends) - 1)])
        for k in np.nonzero(ok)[0]:
            p = pos[k]
            ri = rowidx[p]
            if sg["shoot_id"].iat[k] == team0[p]:
                xgf0[ri] += sg["xGoal"].iat[k]
            else:
                xgf1[ri] += sg["xGoal"].iat[k]
    stints["xgf0"] = xgf0
    stints["xgf1"] = xgf1

    # home team id per game (for a home-ice control), from the shot file's codes
    home = (sh.groupby("gid")["homeTeamCode"].first().map(code2id))
    stints["home_id"] = stints["gid"].map(home)
    return stints


def fit(stints: pd.DataFrame, alpha: float = DEFAULT_ALPHA,
        min_toi_sec: int = DEFAULT_MIN_TOI) -> pd.DataFrame:
    """Fit xG-RAPM; return per-skater off/def/net (xG per 60) for players over a TOI floor.

    ``min_toi_sec`` (default 24000s = 400 min of 5v5) drops cameo players whose single-
    season estimates are mostly noise, mirroring the NBA project's minutes threshold.
    """
    players = sorted(set().union(*stints["skaters0"], *stints["skaters1"]))
    pidx = {p: i for i, p in enumerate(players)}
    n = len(players)

    rows, cols, vals, ys, ws = [], [], [], [], []
    r = 0

    def add_obs(off_set, def_set, xgf, dur, is_home, sw):
        nonlocal r
        for p in off_set:
            rows.append(r); cols.append(pidx[p]); vals.append(1.0)       # offense block
        for p in def_set:
            rows.append(r); cols.append(n + pidx[p]); vals.append(1.0)   # defense block
        rows.append(r); cols.append(2 * n); vals.append(1.0 if is_home else 0.0)  # home
        ys.append(xgf / dur * 3600.0)   # xG per 60 min
        ws.append(dur * sw)             # weight by time on ice x season recency (sw=1 single-season)
        r += 1

    for st in stints.itertuples(index=False):
        sw = getattr(st, "sw", 1.0)     # multi-season pool tags stints with a recency weight
        home0 = st.team0 == st.home_id
        add_obs(st.skaters0, st.skaters1, st.xgf0, st.dur, home0, sw)   # team0 attacking
        add_obs(st.skaters1, st.skaters0, st.xgf1, st.dur, not home0, sw)  # team1 attacking

    X = sparse.csr_matrix((vals, (rows, cols)), shape=(r, 2 * n + 1))
    y = np.array(ys)
    w = np.array(ws)
    model = Ridge(alpha=alpha, fit_intercept=True, solver="sparse_cg")
    model.fit(X, y, sample_weight=w)
    coef = model.coef_

    # per-player 5v5 TOI, to threshold and report
    toi = {p: 0.0 for p in players}
    for st in stints.itertuples(index=False):
        for p in st.skaters0: toi[p] += st.dur
        for p in st.skaters1: toi[p] += st.dur

    out = pd.DataFrame({
        "player_id": players,
        "off": coef[:n],
        "def": -coef[n:2 * n],  # negate so higher = suppresses more opponent xG = better
        "toi_min": [toi[p] / 60 for p in players],
    })
    out["net"] = out["off"] + out["def"]
    out = out[out["toi_min"] * 60 >= min_toi_sec].reset_index(drop=True)
    return out.sort_values("net", ascending=False).reset_index(drop=True)


def season_rapm(start_year: int, alpha: float = DEFAULT_ALPHA) -> pd.DataFrame:
    """End-to-end: reconstruct stints, attach xG, fit RAPM for one season."""
    stints = attach_xg(build_stints(start_year), start_year)
    return fit(stints, alpha=alpha)


DEFAULT_WINDOW = 3       # seasons pooled (this season + 2 prior)
DEFAULT_DECAY = 0.75     # recency weight decay per season back (weight = decay ** seasons_ago)


def pooled_stints(end_year: int, window: int = DEFAULT_WINDOW,
                  decay: float = DEFAULT_DECAY) -> pd.DataFrame:
    """Concatenate xG-attached 5v5 stints over a trailing window ending at ``end_year``.

    Seasons [end_year-window+1 .. end_year] that have a pulled shift chart are stacked, each
    tagged with a recency weight ``decay ** (end_year - season)`` in the ``sw`` column so recent
    seasons count more in the ridge. Multi-season pooling is the Stage-3 fix for single-season
    xG-RAPM over-crediting a player for strong linemates: a player who really drives play keeps
    doing so across different linemates, so pooling separates skill from one season's context.
    """
    frames = []
    for y in range(max(ingest.FIRST_SHIFT_SEASON, end_year - window + 1), end_year + 1):
        if not (ingest.PROC / f"shifts_{y}.parquet").exists():
            continue  # season not pulled (or pre-2010-11 shift floor) -- skip, don't fail
        s = attach_xg(build_stints(y), y)
        s["sw"] = float(decay ** (end_year - y))
        frames.append(s)
    if not frames:
        raise RuntimeError(f"no pulled shift seasons in window ending {ingest.season_str(end_year)}")
    return pd.concat(frames, ignore_index=True)


def pool_rapm(end_year: int, window: int = DEFAULT_WINDOW, decay: float = DEFAULT_DECAY,
              alpha: float = DEFAULT_ALPHA, min_toi_sec: int = DEFAULT_MIN_TOI) -> pd.DataFrame:
    """End-to-end multi-season pooled xG-RAPM ending at ``end_year`` (recency-weighted)."""
    return fit(pooled_stints(end_year, window, decay), alpha=alpha, min_toi_sec=min_toi_sec)
