"""Team aggregation (Stage 4): roll per-skater impact up to a team even-strength rating.

Layer C/D of the architecture. Each skater's xG-RAPM off/def is a per-60 rate impact, so a team's
even-strength rate above/below average is the **minute-weighted mean** of its skaters' impacts
(5 skaters are on the ice at all times, so the factor of 5 is absorbed by the downstream
calibration slope). Validated: the minute-weighted aggregate of single-season RAPM reconstructs
team 5v5 xG-for/against rate at r~0.92 offense / ~0.88 defense; aggregating the *projected*
(prior-season) impacts onto a team's actual roster predicts team 5v5 xG-differential at r~0.65.

This module does the aggregation given a season's roster + 5v5 time-on-ice (from MoneyPuck) and a
set of per-player impacts (contemporaneous RAPM, or a forward ``projection.project``). Goalie GSAx,
special teams (PP/PK), replacement level for uncovered minutes, and the impact->goals->points
calibration are the remaining Stage 4/5 pieces.
"""

from __future__ import annotations

import pandas as pd

from . import ingest

# Replacement level: the mean *actual* 5v5 RAPM of players who had no prior-season projection
# (rookies / sub-floor), measured over 2021-2025 -- slightly below average, as expected. Filling
# uncovered roster minutes with this (rather than dropping them, which rates them at the covered
# mean) improved the projected team-net vs xG-diff correlation ~0.65 -> ~0.74 in 5/5 seasons.
REPLACEMENT_OFF = -0.03
REPLACEMENT_DEF = 0.00


def player_toi(season_start: int) -> pd.DataFrame:
    """player_id -> (team, 5v5 icetime seconds) for a season, from MoneyPuck 5v5 skater rows.

    A player traded mid-season has a row per team; each (player, team) stint is kept so his minutes
    are attributed to the right team.
    """
    sk = pd.read_parquet(ingest.PROC / "moneypuck_skaters.parquet")
    sk = sk[(sk["season_start"] == season_start) & (sk["situation"] == "5on5")]
    out = sk[["playerId", "team", "icetime"]].rename(columns={"playerId": "player_id"})
    out["player_id"] = out["player_id"].astype(int)
    return out


def team_ratings(impacts: pd.DataFrame, season_start: int, *,
                 replacement_off: float = REPLACEMENT_OFF, replacement_def: float = REPLACEMENT_DEF,
                 fill: bool = True) -> pd.DataFrame:
    """Minute-weighted team off/def/net from per-player impacts + that season's 5v5 TOI.

    ``impacts``: a frame with ``player_id`` / ``off`` / ``def`` (contemporaneous RAPM or a forward
    projection). Returns one row per ``team`` with minute-weighted ``off`` / ``def`` / ``net`` and
    ``cover`` (the share of the team's 5v5 minutes that had an impact). By default (``fill=True``)
    roster players without an impact (rookies, sub-floor) get the replacement level, so the
    aggregate is weighted over the team's TOTAL minutes -- validated to beat dropping them
    (``fill=False``, the covered-minute mean). See the report.
    """
    toi = player_toi(season_start)
    d = toi.merge(impacts[["player_id", "off", "def"]], on="player_id", how="left")
    covered = d["off"].notna()
    cover = d.loc[covered, "icetime"].groupby(d["team"]).sum() / d.groupby("team")["icetime"].sum()
    if fill:
        d["off"] = d["off"].fillna(replacement_off)
        d["def"] = d["def"].fillna(replacement_def)
    else:
        d = d[covered]
    d = d.assign(woff=d["off"] * d["icetime"], wdef=d["def"] * d["icetime"])
    g = d.groupby("team").agg(woff=("woff", "sum"), wdef=("wdef", "sum"),
                              ice=("icetime", "sum")).reset_index()
    g["off"] = g["woff"] / g["ice"]
    g["def"] = g["wdef"] / g["ice"]
    g["net"] = g["off"] + g["def"]
    g["cover"] = g["team"].map(cover).fillna(1.0)
    return g[["team", "off", "def", "net", "cover"]].sort_values("net", ascending=False).reset_index(drop=True)


def team_xg_rate(season_start: int) -> pd.DataFrame:
    """Actual team 5v5 xG-for/against per game (the aggregation target), from MoneyPuck teams."""
    t = pd.read_parquet(ingest.PROC / "moneypuck_teams.parquet")
    t = t[(t["season_start"] == season_start) & (t["situation"] == "5on5")].copy()
    t["xgf"] = t["xGoalsFor"] / t["games_played"]
    t["xga"] = t["xGoalsAgainst"] / t["games_played"]
    t["xgd"] = t["xgf"] - t["xga"]
    return t[["team", "xgf", "xga", "xgd"]]
