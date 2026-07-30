"""Roster construction for a projection: movement, rookies, and known absences.

Answers the three things a preseason projection needs that history alone cannot give:

1. **Who is on the team now** -- trades, free agency, retirements. Taken from a live
   roster snapshot (``commonteamroster``), which reflects the current state of the
   league. Retirements need no special handling: a retired player simply is not on a
   roster.

2. **Rookies.** Drafted players have no NBA record, so the impact metric cannot score
   them at all. They get a prior from draft position, fitted on how past picks at that
   slot actually performed as rookies.

3. **Known absences.** A player rehabbing a torn ACL or serving an announced
   suspension is not a statistical question -- it is news. There is no clean free feed
   for it (the best historical source, Pro Sports Transactions, sits behind a
   Cloudflare bot challenge we will not bypass), so this is an explicit manual
   override file rather than something silently guessed.

## An important honesty note about snapshots

A preseason projection is only as current as the day it was run. Trades keep happening
through the season, and a projection made in July does not know about a November deal.
That is inherent to preseason projection, not a fixable defect, but it means the
snapshot date must be recorded alongside any output.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .cache import DATA_DIR, cached_fetch

log = logging.getLogger(__name__)

OVERRIDE_PATH = DATA_DIR / "overrides" / "known_absences.json"

# Draft slots are grouped rather than fitted per-pick: there is not enough history to
# distinguish pick 18 from pick 19, and pretending otherwise would fit noise.
DRAFT_BUCKETS = [(1, 3), (4, 8), (9, 14), (15, 20), (21, 30), (31, 60)]

# What an undrafted or unknown-provenance rookie is worth. Deliberately pessimistic:
# most such players never establish themselves, and the ones who do are the exception
# we should be surprised by rather than assume.
UNDRAFTED_PRIOR_MINUTES = 400.0


def team_roster(team_id: int, season: str) -> pd.DataFrame:
    """Current roster for one team, from the live snapshot."""
    from nba_api.stats.endpoints import commonteamroster
    return cached_fetch(
        "team_roster",
        commonteamroster.CommonTeamRoster,
        {"team_id": team_id, "season": season, "timeout": 60},
    )


def draft_history() -> pd.DataFrame:
    """All draft picks ever, with overall pick number and drafting team."""
    from nba_api.stats.endpoints import drafthistory
    return cached_fetch(
        "draft_history", drafthistory.DraftHistory, {"timeout": 60})


def fit_rookie_priors(impact: pd.DataFrame, draft: pd.DataFrame,
                      *, before_season: int) -> pd.DataFrame:
    """Expected rookie-season impact and minutes by draft-position bucket.

    Fitted only on drafts strictly before `before_season`, so it is safe for
    walk-forward use.

    Rookies who never played are included as zero-minute observations rather than
    dropped. Excluding them would make late picks look far better than they are, by
    conditioning on the ones good enough to earn a roster spot.
    """
    d = draft.copy()
    d["draft_year"] = pd.to_numeric(d["SEASON"], errors="coerce")
    d["pick"] = pd.to_numeric(d["OVERALL_PICK"], errors="coerce")
    d = d.dropna(subset=["draft_year", "pick"])
    d["player_id"] = d["PERSON_ID"].astype("int64")

    # Restrict to drafts whose rookie season we can actually observe. Draft history
    # reaches back to 1947 while impact estimates start much later, so without this
    # filter every pre-coverage pick is silently counted as "never played" -- which
    # put the share of top-3 picks who played at 0.15 and their expected rookie
    # minutes at 276, both wildly wrong.
    first_observable = int(impact["season_start"].min())
    d = d[(d["draft_year"] >= first_observable) & (d["draft_year"] < before_season)]

    # A player's rookie season is the season starting in his draft year.
    rookie = impact[["player_id", "season_start", "impact", "off_impact",
                     "def_impact", "minutes"]].copy()
    merged = d[["player_id", "draft_year", "pick"]].merge(
        rookie, left_on=["player_id", "draft_year"],
        right_on=["player_id", "season_start"], how="left")
    merged["minutes"] = merged["minutes"].fillna(0.0)

    rows = []
    for lo, hi in DRAFT_BUCKETS:
        sub = merged[(merged["pick"] >= lo) & (merged["pick"] <= hi)]
        if sub.empty:
            continue
        played = sub[sub["minutes"] > 0]
        rows.append({
            "pick_lo": lo, "pick_hi": hi, "n": len(sub),
            "share_who_played": float((sub["minutes"] > 0).mean()),
            # Averaged over everyone drafted, including those who never played, so
            # this is the unconditional expectation for a pick in this range.
            "exp_minutes": float(sub["minutes"].mean()),
            "exp_impact": float(played["impact"].mean()) if len(played) else np.nan,
            "exp_off_impact": float(played["off_impact"].mean()) if len(played)
            else np.nan,
            "exp_def_impact": float(played["def_impact"].mean()) if len(played)
            else np.nan,
        })
    return pd.DataFrame(rows)


def rookie_projection(priors: pd.DataFrame, pick: float | None) -> dict[str, float]:
    """Look up the prior for a given draft slot; undrafted falls to the last bucket.

    Carries the offensive and defensive components as well as the total, so the decoupled
    projection can value a rookie's offense and defense separately.
    """
    def _row(row: pd.Series, source: str, minutes: float | None = None) -> dict[str, float]:
        return {"exp_impact": float(row["exp_impact"]),
                "exp_off_impact": float(row.get("exp_off_impact", 0.0)),
                "exp_def_impact": float(row.get("exp_def_impact", 0.0)),
                "exp_minutes": float(row["exp_minutes"]) if minutes is None else minutes,
                "source": source}

    if pick is None or pd.isna(pick):
        return _row(priors.iloc[-1], "undrafted", UNDRAFTED_PRIOR_MINUTES)
    for _, row in priors.iterrows():
        if row["pick_lo"] <= pick <= row["pick_hi"]:
            return _row(row, f"pick {int(row['pick_lo'])}-{int(row['pick_hi'])}")
    return _row(priors.iloc[-1], "beyond-range")


# --- Known absences -----------------------------------------------------------

def load_overrides(path: Path = OVERRIDE_PATH) -> pd.DataFrame:
    """Read manually-entered known absences.

    Expected JSON shape (see `write_override_template`):

        {"snapshot_date": "2026-07-29",
         "absences": [
           {"player_name": "...", "player_id": 1234, "games_missed": 25,
            "reason": "achilles rehab", "source": "team announcement 2026-07-10"}]}

    Returns an empty frame when the file is absent -- a missing override file means
    "no known absences", which is a legitimate state and must not be an error.
    """
    if not path.exists():
        log.info("no override file at %s; assuming no known absences", path)
        return pd.DataFrame(columns=["player_id", "player_name", "games_missed",
                                     "reason", "source"])
    payload = json.loads(path.read_text())
    df = pd.DataFrame(payload.get("absences", []))
    if not df.empty:
        df["player_id"] = pd.to_numeric(df["player_id"], errors="coerce")
        df["games_missed"] = pd.to_numeric(df["games_missed"], errors="coerce")
    return df


def write_override_template(path: Path = OVERRIDE_PATH,
                            snapshot_date: str = "") -> Path:
    """Create a documented, empty override file for hand-editing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    template = {
        "_README": [
            "Known absences for the upcoming season: injuries, suspensions, ",
            "anything already announced. This file is manual on purpose -- there is ",
            "no reliable free feed for forward-looking absences, and guessing would ",
            "be worse than asking. Anything not listed here is handled ",
            "statistically by the availability model instead.",
            "games_missed is the expected number of the team's games the player ",
            "will miss. player_id must match nba_api PERSON_ID.",
        ],
        "snapshot_date": snapshot_date,
        "absences": [],
    }
    path.write_text(json.dumps(template, indent=2))
    log.info("wrote override template to %s", path)
    return path


def apply_overrides(projected: pd.DataFrame, overrides: pd.DataFrame,
                    *, team_games: float = 82.0) -> pd.DataFrame:
    """Cap projected availability using known absences.

    Uses the *minimum* of the statistical projection and the override-implied
    availability, rather than overwriting. If the model already expects a player to
    miss more games than the announcement says, the announcement is a floor on the
    absence, not a ceiling -- a player known to miss 20 games with a long history of
    injury should not be projected healthier than the model already thought.
    """
    if overrides.empty or projected.empty:
        return projected
    out = projected.copy()
    ov = overrides.dropna(subset=["player_id", "games_missed"]).copy()
    implied = 1.0 - (ov["games_missed"] / team_games).clip(0, 1)
    cap = dict(zip(ov["player_id"].astype("int64"), implied))
    out["override_availability"] = out["player_id"].map(cap)
    out["proj_availability"] = np.where(
        out["override_availability"].notna(),
        np.minimum(out["proj_availability"], out["override_availability"]),
        out["proj_availability"])
    out["has_override"] = out["override_availability"].notna()
    return out
