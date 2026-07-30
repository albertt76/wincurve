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


# --- Coach rotation tendency ---------------------------------------------------

def coach_concentration(player_team_seasons: pd.DataFrame, coaches: pd.DataFrame,
                        team_seasons: pd.DataFrame, *, before_season: int,
                        min_seasons: int = 2) -> tuple[dict[int, float], float]:
    """Each head coach's historical top-8 minute share, plus the league mean.

    Minute concentration is substantially a coach trait, not a team one: measured over
    2013-2025 the between-coach variance is 0.00227 against a within-coach variance of
    0.00275, an intraclass-style ratio of 0.45. Tom Thibodeau runs the most concentrated
    rotation in the league at 0.813 over nine seasons against a 0.750 league mean, while
    Mark Daigneault (0.715) and Steve Kerr (0.734) sit below it.

    **MEASURED: the trait is real, but reshaping projected minutes with it does NOT help.**
    End-to-end MAE went 8.343 -> 8.542 when applied to every team. The likely reason is the
    same failure mode as the canonical curve: prior-season minutes ALREADY encode the
    coach's tendency whenever he stayed with the team, so pulling them toward his career
    average replaces specific recent information with an average.

    The case where it *should* still help is a **coaching change**, where prior minutes
    encode the departed coach's preferences instead. That subset is untested -- roughly
    5-8 changes per season over 9 seasons is only ~45-70 team-seasons, thin but not
    hopeless. `coaches` is therefore an opt-in argument to project_team_ratings, defaulting
    to off.

    Coaches with fewer than ``min_seasons`` of history are omitted; callers should fall
    back to the league mean for them, which is the honest treatment for a new coach.
    """
    hc = coaches[coaches["COACH_TYPE"] == "Head Coach"].copy()
    hc["team_id"] = hc["TEAM_ID"].astype("int64")
    hc["season_start"] = hc["SEASON_START"].astype(int)
    hc = hc.sort_values(["team_id", "season_start"]).drop_duplicates(
        ["team_id", "season_start"])

    tg = team_seasons[["team_id", "season_start", "games"]]
    d = player_team_seasons.merge(tg, on=["team_id", "season_start"], how="inner")
    d = d[d["season_start"] < before_season]
    if d.empty:
        return {}, 0.75

    shares = []
    for (tid, season), grp in d.groupby(["team_id", "season_start"]):
        m = np.sort(grp["minutes"].to_numpy(dtype=float))[::-1]
        tot = m.sum()
        if tot > 0:
            shares.append({"team_id": tid, "season_start": season,
                           "top8_share": m[:8].sum() / tot})
    sh = pd.DataFrame(shares)
    if sh.empty:
        return {}, 0.75

    j = sh.merge(hc[["team_id", "season_start", "COACH_ID"]],
                 on=["team_id", "season_start"], how="inner")
    league_mean = float(j["top8_share"].mean())
    counts = j.groupby("COACH_ID")["top8_share"].agg(["mean", "size"])
    keep = counts[counts["size"] >= min_seasons]
    return {int(k): float(v) for k, v in keep["mean"].items()}, league_mean


def reshape_to_concentration(minutes: np.ndarray, target_top8: float,
                             *, tol: float = 0.002, max_iter: int = 40) -> np.ndarray:
    """Rescale a minute vector so its top-8 share matches `target_top8`.

    Applies minutes ** gamma and renormalises, bisecting on gamma. A power transform is
    used rather than a fixed curve because it preserves the *ordering* and the relative
    spacing the prior-season minutes encode, changing only how steeply minutes fall off --
    which is precisely what a coach's rotation tendency governs.
    """
    m = np.asarray(minutes, dtype=float)
    total = m.sum()
    if total <= 0 or len(m) <= 8 or not np.isfinite(target_top8):
        return m

    def top8(gamma: float) -> float:
        w = np.power(np.maximum(m, 1e-9), gamma)
        w = w / w.sum()
        return float(np.sort(w)[::-1][:8].sum())

    lo, hi = 0.2, 5.0
    if top8(lo) > target_top8 or top8(hi) < target_top8:
        # Target unreachable for this roster shape; leave it alone rather than distort.
        return m
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        val = top8(mid)
        if abs(val - target_top8) < tol:
            break
        if val < target_top8:
            lo = mid
        else:
            hi = mid
    w = np.power(np.maximum(m, 1e-9), 0.5 * (lo + hi))
    return w / w.sum() * total


# --- Absence absorption --------------------------------------------------------

# Teams absorb a player's absence at roughly two thirds of what linear minute-weighted
# aggregation implies. Measured 0.65-0.71 walk-forward over 2017-2025, and -- importantly
# -- the SAME factor for stars and for rotation players, so this is a general property of
# how minutes redistribute rather than anything special about stars.
#
# The mechanism is straightforward once stated: when a player sits, his minutes do not go
# to a replacement-level body. They go to the next man in an NBA rotation, who is a real
# player. Charging the whole shortfall at replacement level over-penalises every absence.
#
# This is the quantitative form of the Boston-without-Tatum observation: teams expected to
# collapse without a star routinely do not.
#
# **MEASURED: the effect is real but applying it to our projection does NOT help.**
# Two faithful implementations were tried and both were worse than leaving it alone:
#   (a) availability-blind minutes + per-player discount: best 8.397 at 0.68, but the
#       restructuring itself cost more (1.00 gives 8.415 against 8.343 before the change)
#   (b) availability-scaled minutes + upgraded filler value for freed minutes:
#       monotonically worse -- 1.00 -> 8.365, 0.85 -> 8.392, 0.68 -> 8.427, 0.50 -> 8.470
#
# The likely reason is double-counting. Our impact-to-rating calibration is FITTED on
# historical team-seasons in which players missed their normal share of games, so the
# fitted slope already embodies average absorption. Correcting for it again subtracts a
# penalty that was never actually applied.
#
# Default is therefore 1.0 (charge freed minutes at replacement level, the old behaviour).
# The parameter is retained so the finding is testable rather than merely asserted, and
# because it would matter for a KNOWN long absence, where the season-average calibration
# does not apply -- exactly the Luka-out-for-months case. Untested there for lack of data.
ABSENCE_ABSORPTION = 1.0


def absence_adjusted_impact(impact: np.ndarray | pd.Series,
                            availability: np.ndarray | pd.Series,
                            replacement: float,
                            *, absorption: float = ABSENCE_ABSORPTION):
    """Down-weight a player's value for expected absence, at the measured rate.

    Derivation. Linear aggregation says missing a share (1-a) of games costs the team
    (1-a) * (I - rep), charging the lost minutes at replacement level. The measured cost is
    only ``absorption`` times that, so the minutes are effectively filled at
    ``I - absorption * (I - rep)`` rather than at ``rep``. Folding that back in:

        effective_impact = I - (1 - a) * absorption * (I - rep)

    which correctly reduces to ``I`` for a player available all season, and to the filler
    value for one who never plays. ``absorption = 1`` recovers the old behaviour exactly,
    which is what makes this testable as a single parameter.

    Note this replaces availability's role in *valuation* only. Minutes should now be
    allocated availability-blind, because the shortfall is already priced here; scaling
    minutes by availability as well would double-count the absence.
    """
    imp = np.asarray(impact, dtype=float)
    avail = np.clip(np.asarray(availability, dtype=float), 0.0, 1.0)
    return imp - (1.0 - avail) * absorption * (imp - replacement)


# --- Position-aware minute redistribution --------------------------------------

# A player's minutes do not vanish when he sits, and they do not go to a replacement-level
# body while the bench still has capacity. They go to the players who can actually cover
# his position, until those players run out of headroom.
#
# This is the depth-aware form of the absorption finding. A blanket 0.68 discount failed
# (see ABSENCE_ABSORPTION) precisely because it ignored depth: it charged a deep team and a
# thin team the same reduced penalty. Redistribution produces the depth-dependence for free
# -- a deep roster absorbs freed minutes with real players, a thin one exhausts its bench and
# the remainder falls to replacement level, which is a genuinely larger hit.

# Positions on a big-to-small axis, so similarity is a distance. nba_api reports these seven.
POSITION_AXIS = {"G": 1.0, "G-F": 1.5, "F-G": 1.5, "F": 2.0,
                 "F-C": 2.5, "C-F": 2.5, "C": 3.0}
POSITION_DEFAULT = 2.0
POSITION_SPREAD = 1.5    # how quickly coverage falls off with positional distance
POSITION_FLOOR = 0.08    # in a pinch a coach plays whoever is available

# Hard ceiling on minutes per game played. The league's heaviest-used players sit around
# 36-38; without a cap, redistribution would hand one player 50 minutes a night.
MAX_MPG = 38.0

# A player's role also bounds how far he can stretch. Capping only at MAX_MPG lets a
# 5-minute deep-bench player absorb 30 minutes a night, which does not happen -- coaches
# promote within reason and then look outside the roster. Cap each player at
# ``mpg * STRETCH + BASE``, so a 30-minute starter can reach the hard ceiling while an
# 8-minute player tops out near 18. This is what makes a thin roster genuinely unable to
# cover a star's absence: its bench fills up and the remainder falls to replacement level.
ROLE_STRETCH = 1.5
ROLE_BASE = 6.0


def position_similarity(a, b) -> float:
    """How well a player at position `a` can cover minutes at position `b`.

    Inputs are tolerant on purpose: a missing position arrives as NaN from a left join, and
    an unknown player should fall back to the middle of the axis rather than raise.
    """
    def axis(v) -> float:
        if not isinstance(v, str):
            return POSITION_DEFAULT
        return POSITION_AXIS.get(v.strip().upper(), POSITION_DEFAULT)

    pa, pb = axis(a), axis(b)
    d = abs(pa - pb)
    return max(POSITION_FLOOR, float(np.exp(-(d ** 2) / POSITION_SPREAD)))


def redistribute_minutes(mpg: np.ndarray, avail: np.ndarray,
                         positions: list[str | None], *, team_games: float,
                         max_mpg: float = MAX_MPG,
                         passes: int = 3) -> tuple[np.ndarray, float]:
    """Reallocate minutes freed by expected absence to teammates who can cover them.

    Returns ``(minutes_per_player, unabsorbed_minutes)``. Unabsorbed minutes are the
    genuine shortfall and should be charged at replacement level -- that is the part a thin
    roster cannot cover.

    Each absent player's freed minutes are offered to teammates in proportion to
    ``position_similarity x remaining headroom``, so a guard's minutes go mostly to guards,
    and a player already at the cap takes none. Repeated ``passes`` let minutes spill to the
    next-best coverers when the closest ones fill up, which is what makes a thin roster
    behave differently from a deep one.
    """
    mpg = np.asarray(mpg, dtype=float)
    avail = np.clip(np.asarray(avail, dtype=float), 0.0, 1.0)
    n = len(mpg)
    if n == 0:
        return np.zeros(0), 0.0

    played = mpg * avail                      # minutes per team game actually played
    freed = mpg * (1.0 - avail)               # minutes per team game to reassign
    extra = np.zeros(n)

    sim = np.array([[position_similarity(positions[i], positions[j])
                     for j in range(n)] for i in range(n)])
    np.fill_diagonal(sim, 0.0)                # a player cannot cover his own absence

    pool = freed.copy()
    for _ in range(passes):
        if pool.sum() <= 1e-9:
            break
        role_cap = np.minimum(mpg * ROLE_STRETCH + ROLE_BASE, max_mpg)
        headroom = np.maximum(role_cap - (played + extra), 0.0)
        if headroom.sum() <= 1e-9:
            break
        moved = np.zeros(n)
        for j in range(n):
            if pool[j] <= 1e-9:
                continue
            w = sim[:, j] * headroom
            tot = w.sum()
            if tot <= 1e-9:
                continue
            take = np.minimum(pool[j] * w / tot, headroom)
            moved += take
            pool[j] -= take.sum()
        if moved.sum() <= 1e-9:
            break
        extra += moved

    minutes = (played + extra) * team_games
    return minutes, float(pool.sum() * team_games)
