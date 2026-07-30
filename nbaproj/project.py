"""Stage 4: aggregate projected players into projected team ratings.

## The roster problem, stated honestly

Projecting a team requires knowing who is on it. For a *live* projection that is easy
-- take the current roster snapshot. For a *backtest* it is the single easiest place to
accidentally fake a good result, because the obvious approach (use whoever actually
played that season) hands the model information nobody had in October: midseason
trades, buyout signings, who got cut.

Perfect point-in-time rosters need historical transaction data we do not have. So this
module computes **two** variants and reports both:

- ``early`` -- players who appeared in the team's first N games, with minutes projected
  from prior seasons. Realistic: it knows the opening rotation and nothing later.
- ``actual`` -- the real full-season roster with the real minutes. Deliberately leaky,
  and included precisely because the gap between the two *is* the size of the roster
  knowledge advantage. Reporting only the second would flatter the model; reporting
  only the first would hide how much of the remaining error is roster churn rather
  than talent misjudgement.

Both project player *impact* from prior seasons only. The difference between them is
purely roster and minute knowledge.

``early`` has a known blind spot worth naming: a star injured on opening night does not
appear in the first N games, so the model misses him entirely. Widening the window
trades that error against leaking more midseason movement; 15 games is the compromise.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .aging import aging_curves, build_transitions, project_next_season, replacement_level
from .availability import build_availability, build_features, fit_predict
from .minutes import (
    MINUTES_PER_GAME, assign_rank_minutes, coach_concentration,
    fit_canonical_minutes, reshape_to_concentration,
)

log = logging.getLogger(__name__)

EARLY_GAMES = 15
IMPACT_COLS = ("impact", "off_impact", "def_impact")


def roster_from_early_games(player_game_log: pd.DataFrame, season_start: int,
                            n_games: int = EARLY_GAMES) -> pd.DataFrame:
    """Reconstruct each team's opening rotation from its first `n_games`.

    Game order comes from each team's own sorted game dates, not a league-wide date
    cut, because teams do not all play the same number of games in the first weeks.
    """
    logs = player_game_log[player_game_log["SEASON_START"] == season_start]
    if logs.empty:
        return pd.DataFrame()

    # Rank each team's games chronologically, then keep the first n.
    team_games = logs[["TEAM_ID", "GAME_ID", "GAME_DATE"]].drop_duplicates()
    team_games = team_games.sort_values(["TEAM_ID", "GAME_DATE"])
    team_games["game_no"] = team_games.groupby("TEAM_ID").cumcount() + 1
    early = team_games[team_games["game_no"] <= n_games][["TEAM_ID", "GAME_ID"]]

    sub = logs.merge(early, on=["TEAM_ID", "GAME_ID"], how="inner")
    return sub.groupby(["TEAM_ID", "PLAYER_ID"], as_index=False).agg(
        early_minutes=("MIN", "sum"), early_games=("GAME_ID", "nunique")
    ).rename(columns={"TEAM_ID": "team_id", "PLAYER_ID": "player_id"})


def roster_opening_day(team_rosters: pd.DataFrame, player_game_log: pd.DataFrame,
                       season_start: int) -> pd.DataFrame:
    """Reconstruct opening-day rosters from season rosters minus midseason arrivals.

    Neither source works alone:

    - ``commonteamroster`` gives the **end-of-season** roster (verified: Blake Griffin,
      traded in January 2018, appears on Detroit's 2017-18 roster and not the Clippers').
      Used raw it leaks midseason trades.
    - Game logs miss anyone who never played, which notably excludes a star injured on
      opening night -- a player whose presence is perfectly well known in preseason.

    Combining them fixes both. A player on team X's season roster is treated as a
    midseason arrival, and dropped, if he **played for a different team earlier that same
    season**. Everyone else is kept, including players with zero minutes.

    Residual imperfections, stated rather than hidden:
      - A free agent signed midseason who had not played anywhere else that season is
        wrongly kept. These are typically minimum-contract depth players.
      - A player on the opening roster who was cut before appearing anywhere is wrongly
        dropped. Also low-impact.
      - A player traded *away* midseason correctly stays on his original team, which is
        right for opening day, but his replacement is correctly excluded -- so early-season
        rosters can slightly over-count total minutes. The minute budget handles that.
    """
    ros = team_rosters[team_rosters["season_start"] == season_start]
    logs = player_game_log[player_game_log["SEASON_START"] == season_start]
    if ros.empty or logs.empty:
        return pd.DataFrame()

    # First appearance date per (player, team) this season.
    first = logs.groupby(["PLAYER_ID", "TEAM_ID"], as_index=False)["GAME_DATE"].min()
    first = first.rename(columns={"PLAYER_ID": "player_id", "TEAM_ID": "team_id",
                                  "GAME_DATE": "first_date"})
    # Earliest appearance for ANY team this season, per player.
    earliest = first.groupby("player_id", as_index=False)["first_date"].min().rename(
        columns={"first_date": "season_first_date"})

    r = ros[["team_id", "player_id"]].drop_duplicates().merge(
        first, on=["team_id", "player_id"], how="left").merge(
        earliest, on="player_id", how="left")

    # Midseason arrival: he appeared somewhere this season BEFORE his first game for
    # this team. Players who never played (first_date NaT) are kept by design.
    arrived_later = (
        r["first_date"].notna()
        & r["season_first_date"].notna()
        & (r["first_date"] > r["season_first_date"])
    )
    kept = r[~arrived_later][["team_id", "player_id"]]

    # Players traded AWAY midseason are missing from the end-of-season roster entirely,
    # so the step above loses them from the team they actually started on. Blake Griffin
    # played 33 games for the 2017-18 Clippers before being traded, and appears on
    # nobody's season roster but Detroit's -- without this he vanishes from both.
    #
    # Recover them by adding anyone whose FIRST game of the season was for this team.
    started_here = first.merge(earliest, on="player_id", how="left")
    started_here = started_here[
        started_here["first_date"] <= started_here["season_first_date"]
    ][["team_id", "player_id"]]

    return pd.concat([kept, started_here]).drop_duplicates().reset_index(drop=True)


def fit_rating_scale(impact: pd.DataFrame, player_team_seasons: pd.DataFrame,
                     team_seasons: pd.DataFrame, *, before_season: int,
                     col: str = "impact", target: str = "net_rating") -> tuple[float, float]:
    """Fit the mapping from aggregated team impact to a team rating deviation.

    Theory says the slope should be 5 (a player fills one of five floor spots), but the
    fitted value depends on how hard the impact metric was regularised, so it is
    measured per fold rather than assumed. Uses actual historical minutes, isolating
    the scale question from any minutes-projection error.
    """
    rep = replacement_level(impact, col)
    df = player_team_seasons.merge(
        impact[["player_id", "season_start", col]],
        on=["player_id", "season_start"], how="left")

    rows = []
    for (season, team_id), grp in df.groupby(["season_start", "team_id"]):
        if season >= before_season:
            continue
        mins = grp["minutes"].to_numpy(dtype=float)
        vals = grp[col].to_numpy(dtype=float)
        tot = mins.sum()
        if tot <= 0:
            continue
        ok = ~np.isnan(vals)
        rows.append({
            "season_start": season, "team_id": team_id,
            "agg": float(np.sum(mins[ok] * vals[ok]) + mins[~ok].sum() * rep) / tot,
        })
    agg = pd.DataFrame(rows)
    if len(agg) < 60:
        return 5.0, 0.0

    t = team_seasons.copy()
    t["dev"] = t[target] - t.groupby("season_start")[target].transform("mean")
    if target == "def_rating":
        t["dev"] = -t["dev"]
    j = agg.merge(t[["team_id", "season_start", "dev"]],
                  on=["team_id", "season_start"]).dropna()
    slope, intercept = np.polyfit(j["agg"], j["dev"], 1)
    return float(slope), float(intercept)


def project_team_ratings(
    impact: pd.DataFrame,
    player_team_seasons: pd.DataFrame,
    player_game_log: pd.DataFrame,
    team_seasons: pd.DataFrame,
    ages: pd.DataFrame,
    *,
    target_season: int,
    mode: str = "early",
    team_rosters: pd.DataFrame | None = None,
    coaches: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Projected net-rating deviation for every team in `target_season`.

    Everything used to *value* players comes from seasons strictly before the target.
    `mode` controls only how much roster and minute knowledge is granted -- see the
    module docstring.
    """
    hist = impact[impact["season_start"] < target_season]
    if hist.empty:
        return pd.DataFrame()

    rep = replacement_level(hist, "impact")
    curves = aging_curves(build_transitions(hist, min_minutes=500),
                          ["impact", "off_impact", "def_impact"], corrected=True)

    # Player talent projections, from history only.
    proj = project_next_season(hist, curves, target_season=target_season,
                               skill="impact")
    if proj.empty:
        return pd.DataFrame()
    talent = proj.set_index("player_id")["proj_impact"].to_dict()

    if mode == "actual":
        # Leaky upper bound: real roster, real minutes.
        alloc = player_team_seasons[
            player_team_seasons["season_start"] == target_season
        ][["team_id", "player_id", "minutes"]].copy()
    else:
        if mode == "roster":
            if team_rosters is None:
                raise ValueError("mode='roster' requires team_rosters")
            base = roster_opening_day(team_rosters, player_game_log, target_season)
            if base.empty:
                return pd.DataFrame()
            prior = player_team_seasons[
                player_team_seasons["season_start"] == target_season - 1
            ].groupby("player_id", as_index=False).agg(
                pm=("minutes", "sum"), pg=("games", "sum"))
            prior["prior_mpg"] = prior["pm"] / prior["pg"].clip(lower=1)
            roster = base.merge(prior[["player_id", "prior_mpg"]], on="player_id",
                                how="left")
            # A player with no prior season (rookie, returnee) gets a modest default
            # rather than being dropped, so his minutes still consume team budget.
            roster["early_mpg"] = roster["prior_mpg"].fillna(12.0)
            roster["early_games"] = 1

        else:
            roster = roster_from_early_games(player_game_log, target_season)
            if roster.empty:
                return pd.DataFrame()
            roster["early_mpg"] = roster["early_minutes"] / roster[
                "early_games"].clip(lower=1)

        # Minutes per game from the opening rotation, scaled by projected
        # availability over the full season.
        avail = build_availability(player_team_seasons, team_seasons)
        feats = build_features(avail, ages)
        try:
            av = fit_predict(feats, target_season=target_season)
        except ValueError:
            return pd.DataFrame()
        av_map = av.set_index("player_id")["proj_availability"].to_dict()

        team_games = team_seasons[
            team_seasons["season_start"] == target_season
        ].set_index("team_id")["games"].to_dict()

        roster["avail"] = roster["player_id"].map(av_map).fillna(0.85)
        roster["tg"] = roster["team_id"].map(team_games).fillna(82.0)
        # Prior-season minutes-per-game, scaled by projected availability. Two
        # rank-based alternatives were tried and BOTH measured worse -- see the note on
        # fit_canonical_minutes in minutes.py. Kept simple because simple won.
        roster["minutes"] = roster["early_mpg"] * roster["avail"] * roster["tg"]

        # Optionally reshape each team's minute curve toward its head coach's historical
        # rotation tendency. Concentration is substantially a coach trait (intraclass
        # ratio 0.45), and unlike the league-average curve this keeps the team-specific
        # information while adjusting only the steepness of the falloff. A coach without
        # enough history -- notably any new hire -- falls back to the league mean, which
        # is the honest treatment rather than a guess.
        if coaches is not None and mode == "roster":
            conc, league_mean = coach_concentration(
                player_team_seasons, coaches, team_seasons,
                before_season=target_season)
            hc = coaches[coaches["COACH_TYPE"] == "Head Coach"].copy()
            hc["team_id"] = hc["TEAM_ID"].astype("int64")
            hc["season_start"] = hc["SEASON_START"].astype(int)
            hc = hc[hc["season_start"] == target_season].drop_duplicates("team_id")
            coach_of = dict(zip(hc["team_id"], hc["COACH_ID"]))
            for tid, grp in roster.groupby("team_id"):
                cid = coach_of.get(tid)
                target = conc.get(int(cid), league_mean) if cid is not None \
                    else league_mean
                roster.loc[grp.index, "minutes"] = reshape_to_concentration(
                    grp["minutes"].to_numpy(dtype=float), target)

        # Enforce the team minute budget: scale down when over, never up when under.
        budget = roster["team_id"].map(
            {k: MINUTES_PER_GAME * v for k, v in team_games.items()}).fillna(
            MINUTES_PER_GAME * 82)
        tot = roster.groupby("team_id")["minutes"].transform("sum")
        over = tot > budget
        roster.loc[over, "minutes"] = (
            roster.loc[over, "minutes"] * budget[over] / tot[over])
        alloc = roster[["team_id", "player_id", "minutes"]]

    alloc = alloc.copy()
    alloc["talent"] = alloc["player_id"].map(talent)

    team_games = team_seasons[
        team_seasons["season_start"] == target_season
    ].set_index("team_id")["games"].to_dict()

    rows = []
    for team_id, grp in alloc.groupby("team_id"):
        budget = MINUTES_PER_GAME * team_games.get(team_id, 82)
        mins = grp["minutes"].to_numpy(dtype=float)
        vals = grp["talent"].to_numpy(dtype=float)
        ok = ~np.isnan(vals)
        used = mins.sum()
        # Any part of the budget not accounted for -- unfilled minutes, or players we
        # cannot value -- is charged at replacement level rather than ignored.
        leftover = max(budget - used, 0.0) + mins[~ok].sum()
        agg = (np.sum(mins[ok] * vals[ok]) + leftover * rep) / max(budget, used)
        rows.append({
            "season_start": target_season,
            "team_id": team_id,
            "agg_impact": agg,
            "covered_share": float(mins[ok].sum() / budget),
            "n_players": int(ok.sum()),
        })
    return pd.DataFrame(rows)


def calibrate_projected_ratings(projections: pd.DataFrame,
                                team_seasons: pd.DataFrame,
                                *, target_season: int,
                                target: str = "net_rating") -> tuple[float, float]:
    """Map *projected* aggregate impact to a rating, fitted on projected aggregates.

    This must be fitted on the same kind of quantity it is applied to. Fitting it on
    contemporaneous aggregates instead was a real and costly bug: projected aggregates
    are much less dispersed than contemporaneous ones, because projection deliberately
    shrinks players toward the mean. A slope learned on the wide distribution and
    applied to the narrow one produced predictions with a spread of 1.14 against an
    actual spread of 5.02 -- every team projected near .500, which wrecked error even
    though the underlying ranking correlated 0.60 with reality.

    Only seasons strictly before `target_season` are used, and every projection fed in
    was itself built from data preceding its own season, so this stays walk-forward.
    """
    t = team_seasons.copy()
    t["dev"] = t[target] - t.groupby("season_start")[target].transform("mean")
    if target == "def_rating":
        t["dev"] = -t["dev"]

    train = projections[projections["season_start"] < target_season].merge(
        t[["team_id", "season_start", "dev"]], on=["team_id", "season_start"]).dropna(
        subset=["agg_impact", "dev"])
    if len(train) < 60:
        # Too little history to calibrate; fall back to the theoretical slope of 5.
        return 5.0, 0.0
    slope, intercept = np.polyfit(train["agg_impact"], train["dev"], 1)
    return float(slope), float(intercept)
