"""Stint reconstruction from NHL shift charts -- the on-ice-units input to xG-RAPM.

A "stint" is a maximal interval during which the on-ice personnel is constant. We split
each game at every shift start/end, and keep the intervals that are **5-skaters-a-side
even strength** (goalies excluded via the MoneyPuck goalie set). Each even-strength stint
carries the five skaters on each team and its duration, so a season's shots (MoneyPuck xG)
can be attributed to the exact units that were on the ice -- the hockey analog of the NBA
project's GameRotation stint reconstruction.

Special teams (power play / penalty kill) and 3-on-3 overtime are intentionally dropped
here; they get their own treatment in a later stage.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PERIOD_SECONDS = 1200  # a regulation (and playoff-OT) period is 20 minutes


def _abs_seconds(period: pd.Series, mmss: pd.Series) -> pd.Series:
    """(period, 'MM:SS') -> absolute game seconds. P1 00:00 = 0; P2 00:00 = 1200."""
    parts = mmss.astype(str).str.split(":", expand=True).astype(int)
    return (period.astype(int) - 1) * PERIOD_SECONDS + parts[0] * 60 + parts[1]


def game_stints(shifts_1g: pd.DataFrame, goalie_ids: set[int]) -> pd.DataFrame:
    """Even-strength (5v5) stints for one game.

    Returns one row per stint: gid, start, end, dur (seconds), the two team ids, and
    each team's frozenset of five on-ice skater ids (``skaters_h`` / ``skaters_a`` where
    h/a are just the two teams in id order -- home/away is attached later from the shots).
    """
    df = shifts_1g.copy()
    df["start"] = _abs_seconds(df["period"], df["startTime"])
    df["end"] = _abs_seconds(df["period"], df["endTime"])
    df = df[(df["end"] > df["start"]) & (~df["playerId"].isin(goalie_ids))]  # skaters only
    if df.empty:
        return pd.DataFrame()

    teams = sorted(df["teamId"].unique())
    if len(teams) != 2:
        return pd.DataFrame()
    t0, t1 = teams

    breaks = np.unique(np.concatenate([df["start"].to_numpy(), df["end"].to_numpy()]))
    rows = []
    starts, ends = df["start"].to_numpy(), df["end"].to_numpy()
    pid, tid = df["playerId"].to_numpy(), df["teamId"].to_numpy()
    for b0, b1 in zip(breaks[:-1], breaks[1:]):
        mid = (b0 + b1) / 2.0
        on = (starts <= mid) & (ends > mid)
        s0 = frozenset(pid[on & (tid == t0)])
        s1 = frozenset(pid[on & (tid == t1)])
        if len(s0) == 5 and len(s1) == 5:  # even strength, 5 skaters a side
            rows.append((int(b0), int(b1), int(b1 - b0), t0, s0, t1, s1))

    gid = int(shifts_1g["gameId"].iloc[0])
    out = pd.DataFrame(rows, columns=["start", "end", "dur", "team0", "skaters0",
                                      "team1", "skaters1"])
    out.insert(0, "gid", gid)
    return out
