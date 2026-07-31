"""Aging curves and next-season projection for player impact.

## The survivorship trap, and how this module handles it

The obvious way to build an aging curve -- take every player who appeared in both
season N and N+1, average the change, group by age -- is biased, and the bias grows
with age. Players who decline get cut, so they never produce an N+1 observation. The
34-year-olds you *can* measure are the ones who stayed good enough to keep a job, so
measured decline looks far gentler than reality.

We handle it by imputing rather than dropping. A player who appeared in season N and
then vanished did not stop having a basketball ability; he was replaced by someone at
replacement level. So his N+1 value is set to replacement level instead of being
excluded. Both curves are computed so the size of the bias is visible rather than
assumed -- see `compare_survivorship`.

## Per-skill, not one curve

Aging is modelled separately for each skill because the peaks genuinely differ.
Shooting is widely held to hold up later than athleticism, and defense to be more
experience-driven early. Those are testable claims, and `aging_curves` reports the
peak age per skill so they can be checked rather than assumed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Skills to build separate curves for. `impact` is the headline; the rest exist to
# test whether peaks really differ by skill.
SKILLS = ["impact", "off_impact", "def_impact", "ts_pct", "pts_p100", "ast_p100",
          "fg3m_p100", "dreb_p100", "blk_p100", "stl_p100", "tov_p100"]

# Ages with enough data to say anything. Outside this the samples are tiny and the
# curve is noise.
AGE_MIN, AGE_MAX = 20, 37

# Players in this minutes band are the marginal NBA rotation: the players a team
# turns to when someone is hurt. Their average impact defines replacement level.
REPLACEMENT_MIN_BAND = (250, 750)

# Shrinkage strength, in minutes: how much of a player's own record it takes to
# outweigh the prior. Tuned by walk-forward sweep (see scripts/stage2b_report.py),
# where 200 minimised error and the surrounding values 100-400 were all close.
#
# This was originally set to 1200 while shrinking toward *replacement level*, and
# that combination was actively harmful -- it dragged established rotation players
# toward a fringe-player baseline and made the projection worse than simply reusing
# last season. Both parts mattered: the strength was ~6x too aggressive, and
# replacement level is the wrong target for anyone who is not a fringe player.
SHRINK_MINUTES = 200.0


def replacement_level(impact: pd.DataFrame, col: str = "impact") -> float:
    """Impact of a marginal rotation player, in points per 100 possessions.

    This is the value an absent player's minutes actually get filled by, which makes
    it the right imputation for a player who left the league and the right baseline
    for injury adjustment later.
    """
    lo, hi = REPLACEMENT_MIN_BAND
    band = impact[(impact["minutes"] >= lo) & (impact["minutes"] <= hi)]
    if band.empty:
        return float("nan")
    return float(np.average(band[col].dropna()))


def build_transitions(
    impact: pd.DataFrame,
    skills: list[str] | None = None,
    *,
    min_minutes: int = 500,
) -> pd.DataFrame:
    """Pair each player-season with the following season, imputing dropouts.

    Returns one row per (player, season N) with `<skill>` and `<skill>_next`, plus a
    `dropped_out` flag. The final season in the data is excluded, since everyone
    trivially has no following season there.

    `min_minutes` gates only season N. Season N+1 is deliberately ungated: requiring
    a big N+1 workload would re-introduce exactly the survivorship bias we are
    trying to remove, because declining players get fewer minutes.
    """
    skills = skills or SKILLS
    last_season = int(impact["season_start"].max())

    cur = impact[
        (impact["minutes"] >= min_minutes)
        & (impact["season_start"] < last_season)
    ][["player_id", "player_name", "season_start", "age", "experience",
       "minutes"] + skills].copy()

    nxt = impact[["player_id", "season_start", "minutes"] + skills].copy()
    nxt["season_start"] -= 1
    nxt = nxt.rename(columns={"minutes": "minutes_next",
                              **{s: f"{s}_next" for s in skills}})

    out = cur.merge(nxt, on=["player_id", "season_start"], how="left")
    out["dropped_out"] = out["minutes_next"].isna()

    # Impute the dropouts rather than discarding them. The rule is
    # min(replacement level, last observed value), not simply replacement level:
    # a fringe player already below replacement who then leaves the league must not
    # be recorded as having *improved* by leaving. Naive replacement-level imputation
    # does exactly that, and it flips the sign of the correction at old ages, where
    # most exits happen.
    for skill in skills:
        rep = replacement_level(impact, skill)
        fallback = np.minimum(out[skill].to_numpy(dtype=float), rep)
        out[f"{skill}_next_imputed"] = out[f"{skill}_next"].fillna(
            pd.Series(fallback, index=out.index))

    out["age_next"] = out["age"] + 1
    return out


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    ok = ~np.isnan(values) & ~np.isnan(weights) & (weights > 0)
    if not ok.any():
        return np.nan
    return float(np.average(values[ok], weights=weights[ok]))


def aging_curves(
    transitions: pd.DataFrame,
    skills: list[str] | None = None,
    *,
    corrected: bool = True,
) -> pd.DataFrame:
    """Average year-over-year change in each skill, by age.

    Observations are weighted by the harmonic mean of the two seasons' minutes, the
    standard choice for aging work: it discounts a pairing where either season is a
    small sample, rather than letting a 300-minute season carry the same weight as a
    2,500-minute one.

    With `corrected=True`, players who left the league contribute a decline to
    replacement level. With `corrected=False` they are dropped, reproducing the
    naive (survivor-only) curve.
    """
    skills = skills or SKILLS
    suffix = "_next_imputed" if corrected else "_next"

    rows = []
    for age, sub in transitions.groupby(transitions["age"].round().astype("Int64")):
        if age is pd.NA or not (AGE_MIN <= int(age) <= AGE_MAX):
            continue
        rec: dict[str, object] = {"age": int(age), "n": len(sub)}
        m1 = sub["minutes"].to_numpy(dtype=float)
        m2 = sub["minutes_next"].to_numpy(dtype=float)
        if not corrected:
            keep = ~np.isnan(m2)
        else:
            # A dropout played zero minutes; harmonic mean would be 0, which would
            # silently zero out its weight and undo the correction. Give it the
            # weight its final real season earned instead.
            m2 = np.where(np.isnan(m2), m1, m2)
            keep = np.ones(len(sub), dtype=bool)
        with np.errstate(divide="ignore", invalid="ignore"):
            hmean = 2 * m1 * m2 / (m1 + m2)
        w = np.where(keep, hmean, np.nan)
        rec["weight"] = float(np.nansum(w))
        for skill in skills:
            delta = sub[f"{skill}{suffix}"].to_numpy(dtype=float) - \
                sub[skill].to_numpy(dtype=float)
            rec[f"d_{skill}"] = _weighted_mean(delta, w)
        rows.append(rec)

    return pd.DataFrame(rows).sort_values("age").reset_index(drop=True)


def peak_ages(curves: pd.DataFrame, skills: list[str] | None = None) -> pd.DataFrame:
    """Age at which each skill stops improving.

    Found by cumulating the year-over-year deltas into a level curve and taking its
    maximum. Reading the peak off the cumulative curve rather than off the deltas
    avoids being thrown by one noisy age bucket.
    """
    skills = skills or SKILLS
    rows = []
    for skill in skills:
        col = f"d_{skill}"
        if col not in curves:
            continue
        sub = curves[["age", col]].dropna()
        if len(sub) < 5:
            continue
        level = sub[col].cumsum()
        rise = float(level.max() - level.iloc[0])
        peak_idx = int(np.argmax(level.to_numpy()))
        rows.append({
            "skill": skill,
            "peak_age": int(sub["age"].iloc[peak_idx]),
            "total_rise_to_peak": rise,
            "decline_after_peak": float(level.iloc[-1] - level.max()),
            # When a skill never rises, the "peak" is just the youngest age bucket,
            # which is an artifact of taking an argmax over a monotonically falling
            # curve -- not a real developmental peak. Flag it so the number is not
            # reported as though the skill peaks in a player's early twenties.
            "monotonic_decline": bool(rise < 0.05 and peak_idx <= 1),
        })
    return pd.DataFrame(rows)


def compare_survivorship(transitions: pd.DataFrame,
                         skill: str = "impact") -> pd.DataFrame:
    """Naive vs dropout-corrected aging curve, to size the survivorship bias."""
    naive = aging_curves(transitions, [skill], corrected=False)
    corr = aging_curves(transitions, [skill], corrected=True)
    out = naive[["age", "n", f"d_{skill}"]].rename(
        columns={f"d_{skill}": "naive_delta"}).merge(
        corr[["age", f"d_{skill}"]].rename(
            columns={f"d_{skill}": "corrected_delta"}), on="age")
    out["bias"] = out["naive_delta"] - out["corrected_delta"]
    return out


def project_next_season(
    impact: pd.DataFrame,
    curves: pd.DataFrame,
    *,
    target_season: int,
    skill: str = "impact",
    recency_weights: tuple[float, ...] = (5.0, 3.0, 2.0),
    shrink_minutes: float | None = None,
    shrink_toward: str = "age_mean",
) -> pd.DataFrame:
    """Project each player's `skill` for `target_season`.

    Three steps, in the order that matters:

    1. **Blend recent seasons.** More weight on the most recent, since a single
       season is a noisy read on ability.
    2. **Shrink toward a prior.** Weight of evidence is measured in minutes:
       `shrink_minutes` is how many minutes it takes for the player's own record to
       outweigh the prior. A 300-minute player is mostly prior; a 2,500-minute player
       is mostly himself. This is what stops small-sample players being projected as
       stars.

       `shrink_toward` defaults to `"age_mean"` -- the league-average impact for
       players of that age. Shrinking toward `"replacement"` instead is available but
       measurably worse for anyone who is not a fringe player, since it pulls
       established rotation players down toward a baseline they have long since
       cleared.
    3. **Apply the aging delta** for the age he will be.

    Uses only seasons strictly before `target_season`, so it is safe for
    walk-forward evaluation.
    """
    if shrink_minutes is None:
        shrink_minutes = SHRINK_MINUTES  # read the module global at CALL time, not def time

    hist = impact[impact["season_start"] < target_season]
    if hist.empty:
        return pd.DataFrame()

    rep = replacement_level(hist, skill)
    age_mean = hist.groupby(hist["age"].round())[skill].mean().to_dict()
    delta_by_age = dict(zip(curves["age"], curves[f"d_{skill}"]))

    rows = []
    for player_id, grp in hist.groupby("player_id"):
        grp = grp.sort_values("season_start", ascending=False)
        # Only consider players who were around recently; a player last seen four
        # seasons ago is not on a roster now.
        if grp["season_start"].iloc[0] < target_season - 2:
            continue

        num = den = 0.0
        for w, (_, row) in zip(recency_weights, grp.iterrows()):
            val, mins = row[skill], row["minutes"]
            if pd.isna(val) or pd.isna(mins) or mins <= 0:
                continue
            num += w * mins * val
            den += w * mins
        if den == 0:
            continue

        blended = num / den
        # Shrinkage in minutes units: den/mean(weights) converts the weighted
        # minute total back to an effective sample size.
        eff_min = den / np.mean(recency_weights[:len(grp)])

        latest = grp.iloc[0]
        age_next = latest["age"] + (target_season - latest["season_start"])
        if shrink_toward == "replacement":
            prior = rep
        else:
            # Fall back to the player's own blend rather than to replacement level
            # when an age has no league history, so a missing bucket cannot silently
            # drag him down.
            prior = age_mean.get(round(age_next), blended)
        shrunk = ((eff_min * blended + shrink_minutes * prior)
                  / (eff_min + shrink_minutes)) if shrink_minutes > 0 else blended
        delta = delta_by_age.get(int(round(age_next)), 0.0)
        if pd.isna(delta):
            delta = 0.0

        rows.append({
            "player_id": player_id,
            "player_name": latest["player_name"],
            "season_start": target_season,
            "age": age_next,
            "last_seen": latest["season_start"],
            "blended": blended,
            "shrunk": shrunk,
            "aging_delta": delta,
            f"proj_{skill}": shrunk + delta,
            "evidence_minutes": eff_min,
        })

    return pd.DataFrame(rows)
