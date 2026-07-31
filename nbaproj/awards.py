"""All-NBA / All-Defensive honors as an eye-test display layer.

``scripts/pull_awards.py`` pulls each candidate player's All-NBA and All-Defensive selections
(``data/processed/player_awards.parquet``). We surface the most recent selection as of a
projection -- STRICTLY from seasons before the one projected, so it is a prior-year signal,
never contemporaneous.

**These are a display layer, not a projection input.** Two walk-forward tests said so:

- Prior-year All-Defense does not improve out-of-sample team-defense prediction beyond the
  box + rim/hustle + RAPM metric (corr 0.733 -> 0.726, err 1.440 -> 1.490, worse). At the team
  level the honor is redundant -- a team's defense is already the sum of its players' countable
  events, which the metric measures -- and the out-of-line correction to def_impact was neutral
  in the win gate. Kept as a flag because it *does* pick out the perimeter stoppers the metric
  under-credits at the PLAYER level (Herbert Jones, Holiday, Anunoby, Brooks all rate <= 0).
- All-NBA is largely an OFFENSIVE / reputational honor. The metric rates several All-NBA guards
  low, but for the right reason -- Jalen Brunson is +2.09 offense / -2.01 defense, a correct
  two-way wash; boosting his impact toward his All-NBA status would credit defense he does not
  play. So All-NBA moves no numbers; it is shown as recognition context only.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ALL_DEF = "All-Defensive Team"
ALL_NBA = "All-NBA"


def load_honors(proc_dir: str | Path) -> pd.DataFrame | None:
    """Load the honors table, or None if it has not been pulled yet."""
    p = Path(proc_dir) / "player_awards.parquet"
    return pd.read_parquet(p) if p.exists() else None


def _recent(honors: pd.DataFrame, description: str, before_season: int,
            lookback: int) -> dict[int, dict]:
    """player_id -> {yr, team, n} for the most recent `description` selection strictly before
    `before_season` and within `lookback` seasons. `n` is the player's career count of that
    honor prior to `before_season`. `before_season` is the season_start being projected."""
    all_desc = honors[(honors["DESCRIPTION"] == description)
                      & (honors["season_start"] < before_season)]
    window = all_desc[all_desc["season_start"] >= before_season - lookback]
    career = all_desc.groupby("player_id").size().to_dict()
    out: dict[int, dict] = {}
    for pid, g in window.groupby("player_id"):
        r = g.loc[g["season_start"].idxmax()]
        team = int(r["team_number"]) if pd.notna(r["team_number"]) else None
        out[int(pid)] = {"yr": int(r["season_start"]), "team": team,
                         "n": int(career.get(pid, 0))}
    return out


def honor_lookup(honors: pd.DataFrame | None, before_season: int, *,
                 lookback: int = 3) -> dict[str, dict[int, dict]]:
    """Most-recent All-Defensive and All-NBA selections as of `before_season` (walk-forward).

    Returns ``{"all_def": {player_id: {yr, team, n}}, "all_nba": {...}}``. Empty if no honors
    table. `lookback` bounds how many seasons back still counts as a current recognition.
    """
    if honors is None:
        return {"all_def": {}, "all_nba": {}}
    return {"all_def": _recent(honors, ALL_DEF, before_season, lookback),
            "all_nba": _recent(honors, ALL_NBA, before_season, lookback)}
