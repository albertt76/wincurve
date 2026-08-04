"""Forward projection of skater impact (Stage 3, step 4) -- the bridge from measurement to a
projected next-season talent, and the input Stage 4 team aggregation will consume.

Given a current-talent estimate ``rapm.talent(end_year)`` (multi-season pooled xG-RAPM + box
offensive prior), project each skater's impact for the NEXT season (``end_year + 1``) in three steps:

1. **Age** the estimate forward one year: add the smoothed per-skill aging delta at the player's
   current age (``nhl.aging``). Its measured one-year predictive value is ~0 (the deltas are small),
   but it is structurally correct -- an aging veteran should project to decline -- and does not hurt.
2. **Regress to the mean** by the measured year-over-year persistence, per skill. RAPM is centered at
   ~0, so this is ``projected = beta * aged_talent``. Offense persists far more than defense.
3. (Replacement level and minutes/availability are Stage 3b / Stage 4, not here.)

**Persistence, measured walk-forward** (regress actual single-season net in Y on aged talent in Y-1,
TOI-weighted, 6 folds 2019-2024): **offense beta ~0.62, defense beta ~0.31**. Defensive RAPM is
about half as persistent as offensive -- the NHL analog of the NBA project's "defense is the weakest,
least-predictive metric", now quantified. Intercepts are ~0 (RAPM is centered), so none is applied.
"""

from __future__ import annotations

import pandas as pd

from . import aging, rapm

# Year-over-year persistence per skill (TOI-weighted OLS slope, measured 6 folds; see module docstring).
PERSISTENCE_OFF = 0.62
PERSISTENCE_DEF = 0.31


def project(end_year: int, window: int = rapm.DEFAULT_WINDOW, decay: float = rapm.DEFAULT_DECAY,
            alpha: float = rapm.DEFAULT_ALPHA, box_weight: float = rapm.BOX_OFFENSE_WEIGHT,
            beta_off: float = PERSISTENCE_OFF, beta_def: float = PERSISTENCE_DEF) -> pd.DataFrame:
    """Projected per-skater impact for season ``end_year + 1``: aged, mean-regressed talent.

    Returns one row per skater with projected ``off`` / ``def`` / ``net`` (xG per 60, 5v5), plus
    ``age`` and the pre-projection ``talent_net`` for transparency. Players without a birthdate
    (so no age) are aged by 0 -- their talent is still regressed to the mean.
    """
    tal = rapm.talent(end_year, window=window, decay=decay, alpha=alpha, box_weight=box_weight)
    bd = aging.load_birthdates()
    out = tal.merge(bd, on="player_id", how="left")
    out["age"] = aging.age_at(out["birth"], pd.Series(end_year, index=out.index))
    out["talent_net"] = out["net"]

    # 1. age forward one year (per skill); players with no age keep their talent (delta 0)
    has_age = out["age"].notna()
    aged_off, aged_def = out["off"].copy(), out["def"].copy()
    if has_age.any():
        a = out.loc[has_age]
        aged_off.loc[has_age] = aging.apply_delta(a["off"], a["age"], "off", alpha=alpha).values
        aged_def.loc[has_age] = aging.apply_delta(a["def"], a["age"], "def", alpha=alpha).values

    # 2. regress to the mean (0) by measured per-skill persistence
    out["off"] = beta_off * aged_off
    out["def"] = beta_def * aged_def
    out["net"] = out["off"] + out["def"]
    return out.drop(columns=["birth"]).sort_values("net", ascending=False).reset_index(drop=True)
