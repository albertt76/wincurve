"""Minute allocation under the team budget, with replacement level.

## Why the budget constraint is the whole point

Every team plays exactly 240 player-minutes per game (5 players x 48 minutes, plus
overtime). That constraint is not bookkeeping -- it is the mechanism that makes team
aggregation work, and it produces the right behaviour for free:

  - Minutes freed by an injury are absorbed by someone, automatically.
  - A team that signs a star does not gain minutes, it *reallocates* them, so the
    incumbent's minutes fall.
  - A thin roster's leftover minutes go to replacement-level players, which is what
    actually happens.

## What replacement level does and does not fix

It was introduced expecting to explain the Stage 2 slope anomaly (7.7 against a
predicted 5.0). **It does not.** Covered minute share turned out to be 98.1%, and
closing the remaining 1.9% at replacement level moved the slope only 7.73 -> 7.88.
The real cause was ridge over-regularisation (see impact.py).

Replacement level is kept because it is independently necessary and correct:

  - It closes the minute budget, so weights genuinely sum to one.
  - It is what makes injury adjustment coherent -- an absent player's minutes have to
    go *somewhere*, and "to a replacement-level player" is both the honest default
    and what actually happens.
  - It gives thin rosters the penalty they deserve instead of quietly redistributing
    their minutes to players who were never going to absorb them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MINUTES_PER_GAME = 240.0  # 5 players x 48 minutes


def project_minutes(
    roster: pd.DataFrame,
    *,
    team_games: float = 82.0,
    minutes_col: str = "proj_mpg",
    availability_col: str = "proj_availability",
) -> pd.DataFrame:
    """Allocate each team's minute budget across its roster.

    `roster` needs `team_id`, `player_id`, a projected minutes-per-game column and a
    projected availability column.

    Raw demand is `availability x minutes-per-game x team games`. Because those are
    projected independently per player, the team total will not land on the budget, so
    it is rescaled. The two directions are not symmetric:

    - **Over budget** -- scale every player down proportionally. A coach facing more
      capable players than minutes distributes the squeeze.
    - **Under budget** -- do *not* scale up. Inflating a thin roster's real players
      would credit them with minutes they were never going to play. The shortfall
      becomes replacement-level minutes instead.
    """
    df = roster.copy()
    budget = MINUTES_PER_GAME * team_games

    df["raw_minutes"] = (
        df[availability_col].clip(0, 1) * df[minutes_col].clip(lower=0) * team_games)

    out = []
    for team_id, grp in df.groupby("team_id"):
        grp = grp.copy()
        total = grp["raw_minutes"].sum()
        if total > budget and total > 0:
            grp["proj_minutes"] = grp["raw_minutes"] * budget / total
            grp["replacement_minutes"] = 0.0
        else:
            grp["proj_minutes"] = grp["raw_minutes"]
            # Attributed to the team, not to any player.
            grp["replacement_minutes"] = 0.0
            grp.loc[grp.index[0], "replacement_minutes"] = budget - total
        grp["team_id"] = team_id
        out.append(grp)

    res = pd.concat(out, ignore_index=True)
    res["minute_share"] = res["proj_minutes"] / budget
    return res


def aggregate_team_impact(
    allocation: pd.DataFrame,
    replacement_impact: float,
    *,
    impact_cols: tuple[str, ...] = ("impact", "off_impact", "def_impact"),
    team_games: float = 82.0,
) -> pd.DataFrame:
    """Minute-weighted team impact, with the uncovered remainder at replacement level.

    Returns one row per team. Weights sum to one *including* the replacement share,
    which is what makes the result comparable to a team rating deviation.
    """
    budget = MINUTES_PER_GAME * team_games
    rows = []
    for team_id, grp in allocation.groupby("team_id"):
        rep_min = float(grp["replacement_minutes"].sum())
        rec = {"team_id": team_id,
               "covered_share": float(grp["proj_minutes"].sum() / budget),
               "replacement_share": rep_min / budget}
        for col in impact_cols:
            if col not in grp:
                continue
            vals = grp[col].to_numpy(dtype=float)
            mins = grp["proj_minutes"].to_numpy(dtype=float)
            ok = ~np.isnan(vals)
            # Players missing an impact estimate are folded into the replacement
            # pool rather than dropped, so the budget still closes.
            covered = float(np.sum(mins[ok] * vals[ok]))
            missing_min = float(np.sum(mins[~ok]))
            rec[col] = (covered + (rep_min + missing_min) * replacement_impact) / budget
        rows.append(rec)
    return pd.DataFrame(rows)


def historical_allocation(
    player_team_seasons: pd.DataFrame,
    impact: pd.DataFrame,
    replacement_impact: float,
    *,
    impact_cols: tuple[str, ...] = ("impact", "off_impact", "def_impact"),
) -> pd.DataFrame:
    """Aggregate *actual* historical minutes to team impact.

    This isolates the aggregation step from projection error: it uses minutes that
    really happened, so any remaining mismatch against team ratings is a property of
    the impact metric and the replacement treatment, not of a minutes forecast. It is
    the direct test of whether the Stage 2 slope defect is fixed.
    """
    df = player_team_seasons.merge(
        impact[["player_id", "season_start", *impact_cols]],
        on=["player_id", "season_start"], how="left")

    rows = []
    for (season, team_id), grp in df.groupby(["season_start", "team_id"]):
        total_min = float(grp["minutes"].sum())
        if total_min <= 0:
            continue
        rec = {"season_start": season, "team_id": team_id, "total_minutes": total_min}
        for col in impact_cols:
            vals = grp[col].to_numpy(dtype=float)
            mins = grp["minutes"].to_numpy(dtype=float)
            ok = ~np.isnan(vals)
            rec[f"{col}_covered_share"] = float(mins[ok].sum() / total_min)
            rec[col] = float(
                np.sum(mins[ok] * vals[ok]) + mins[~ok].sum() * replacement_impact
            ) / total_min
        rows.append(rec)
    return pd.DataFrame(rows)


# --- Rank-based minute projection ---------------------------------------------

MAX_ROTATION_RANK = 15


def fit_canonical_minutes(player_team_seasons: pd.DataFrame,
                          team_seasons: pd.DataFrame,
                          *, before_season: int) -> dict[int, float]:
    """Average minutes per TEAM game by a player's minutes rank within his team.

    NBA rotations are far more canonical than they look: measured over 2013-2025 the
    top-ranked player averages 30.9 minutes per team game with a standard deviation of
    just 3.25 (11% relative), and the top 14 ranks sum to 230.6 of the 240-minute budget.
    That regularity makes rank a strong prior for a player whose role is changing -- a
    star traded to a new team keeps a star's rank even though his prior minutes came in a
    different context.

    Expressed per TEAM game rather than per game played, so typical absence is already
    baked into the curve. Applying a full availability multiplier on top would therefore
    double-count it; scale only by availability *relative to* the league norm.

    **MEASURED RESULT: this does not work, and is retained only as documentation.**
    Substituting the canonical curve for prior-season minutes made end-to-end error
    clearly worse (MAE 9.14 against 8.44), and a hybrid using the curve only for players
    with no prior season was also worse (8.61). The curve's regularity is real but it is
    an average, and averaging away how a *particular* team distributes minutes discards
    genuine signal -- some teams really do run a 36-minute star while others spread the
    load, and that difference changes team strength. Prior-season minutes-per-game with a
    flat default for newcomers remains the best measured option.
    """
    tg = team_seasons[["team_id", "season_start", "games"]].rename(
        columns={"games": "team_games"})
    d = player_team_seasons.merge(tg, on=["team_id", "season_start"], how="inner")
    d = d[(d["season_start"] < before_season) & (d["team_games"] > 0)]
    if d.empty:
        return {}
    d = d.copy()
    d["mpg"] = d["minutes"] / d["team_games"]
    d["rank"] = d.groupby(["team_id", "season_start"])["minutes"].rank(
        ascending=False, method="first")
    curve = d[d["rank"] <= MAX_ROTATION_RANK].groupby("rank")["mpg"].mean()
    return {int(k): float(v) for k, v in curve.items()}


def assign_rank_minutes(roster: pd.DataFrame, curve: dict[int, float],
                        *, rank_col: str, team_games: float,
                        mean_availability: float = 0.85) -> pd.Series:
    """Minutes for each player from the canonical curve, by within-team rank.

    Players beyond the rotation depth get the deepest rank's value, not zero: a 16th man
    does play occasionally, and zeroing him would push his minutes into the
    replacement-level pool where they are valued slightly differently.
    """
    if not curve:
        return pd.Series(0.0, index=roster.index)
    deepest = min(curve.values())
    ranks = roster.groupby("team_id")[rank_col].rank(
        ascending=False, method="first")
    mpg = ranks.map(lambda r: curve.get(int(r), deepest))

    if "proj_availability" in roster:
        # Relative to the league norm only -- the curve already contains average absence.
        adj = (roster["proj_availability"] / mean_availability).clip(0.4, 1.15)
    else:
        adj = 1.0
    return mpg * adj * team_games
