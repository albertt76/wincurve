"""Pre-check (defensive roadmap item #3): does nba_api MATCHUP data give a usable
perimeter-CONTAINMENT feature? Verdict: NO -- SKIP the feature (keep the data).

MatchupsRollup counts who guarded whom and what they allowed as the PRIMARY defender -- the one
public source for perimeter containment (rim/hustle/shot-zone tracking can't see it). The pull is
cheap and clean (1 call/season, ToS-safe, wired into nbaproj.ingest.matchups / data inventory).

But the containment feature is too noisy to use. Candidate features per (defender, season),
aggregated over the offensive-position rows and gated to rotation defenders (>=800 partial poss):
  m_fgpct   = FGM/FGA allowed  (the containment signal)
  m_tovp100 = 100 * TOV / poss (forced turnovers as primary defender)

Result (2026-08-02), year-over-year stability -- the exact filter that killed DRAYMOND:
  (1) single-season adjacent YoY m_fgpct:            r2 = 0.052   (noise)
  (2) 2yr-pooled decayed m_fgpct -> next disjoint yr: r2 = 0.068
  (3) 3yr-pooled decayed m_fgpct -> next disjoint yr: r2 = 0.082
  (ref) single-season YoY m_tovp100:                 r2 = 0.347

m_fgpct stays r2 ~0.05-0.08 even with multi-season pooling -- far below a usable ~0.25 -- because
nearest-defender labelling has no arm position or facing direction, so a jump-shot "contest" is
inherently high-variance (identical root cause to the rejected shot-zone perimeter defense). The
one stable feature, m_tovp100, largely restates existing steals (stl_p100) and its high stability
is role, not new skill. A third feature the scan flagged, m_ptsp100 (points allowed per matchup
poss), is ASSIGNMENT-ENDOGENOUS -- it rates weak defenders "elite" for being hidden on weak
offensive players (Trae Young / Anthony Edwards score elite) -- and is deliberately not tested.

So no matchup feature is worth a 5000-sim gate. The DATA is kept (nbaproj.ingest.matchups,
data/processed/matchups.parquet) for future work (e.g. richer tracking, or a forced-turnover
feature if it ever clears stability).

    python scripts/precheck_matchup_defense.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from nbaproj.ingest import FIRST_MATCHUP_SEASON, matchups
from nbaproj.cache import season_str  # noqa: E402

PROC = Path("data/processed")
LAST = 2025
MIN_POSS = 800


def _load() -> pd.DataFrame:
    """Prefer the materialized parquet; else pull each season through the throttled cache."""
    p = PROC / "matchups.parquet"
    if p.exists():
        df = pd.read_parquet(p)
        if "season_start" not in df.columns and "SEASON_START" in df.columns:
            df = df.rename(columns={"SEASON_START": "season_start"})
        return df
    frames = []
    for s in range(FIRST_MATCHUP_SEASON, LAST + 1):
        d = matchups(season_str(s))
        if not d.empty:
            d = d.copy(); d["season_start"] = s; frames.append(d)
    return pd.concat(frames, ignore_index=True)


def per_season(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["season_start", "DEF_PLAYER_ID"], as_index=False).agg(
        poss=("PARTIAL_POSS", "sum"), pts=("PLAYER_PTS", "sum"),
        fgm=("MATCHUP_FGM", "sum"), fga=("MATCHUP_FGA", "sum"), tov=("MATCHUP_TOV", "sum"))
    g = g[g["poss"] >= MIN_POSS].copy()
    g["m_fgpct"] = g["fgm"] / g["fga"].clip(lower=1)
    g["m_tovp100"] = 100 * g["tov"] / g["poss"].clip(lower=1)
    return g.rename(columns={"DEF_PLAYER_ID": "player_id"})


def _r2(x, y):
    return (float(np.corrcoef(x, y)[0, 1] ** 2), len(x)) if len(x) >= 20 else (float("nan"), len(x))


def main() -> int:
    seas = per_season(_load())
    print(f"rotation defenders (>= {MIN_POSS} poss): {len(seas)} player-seasons, "
          f"{seas.season_start.nunique()} seasons")

    piv = seas.pivot_table(index="player_id", columns="season_start", values="m_fgpct")
    xs, ys = [], []
    for s in range(FIRST_MATCHUP_SEASON, LAST):
        both = piv[[s, s + 1]].dropna()
        xs += both[s].tolist(); ys += both[s + 1].tolist()
    print(f"\n(1) single-season adjacent YoY m_fgpct:            r2={_r2(np.array(xs), np.array(ys))[0]:.3f}")

    for span, w in [("2yr", {0: 1.0, 1: 0.7}), ("3yr", {0: 1.0, 1: 0.7, 2: 0.5})]:
        px, py = [], []
        offs = sorted(w)
        for pid, g in seas.groupby("player_id"):
            gi = g.set_index("season_start")
            for T in range(FIRST_MATCHUP_SEASON + max(offs), LAST):
                if not all(y in gi.index for y in [T - o for o in offs] + [T + 1]):
                    continue
                num = sum(w[o] * gi.loc[T - o, "fgm"] for o in offs)
                den = sum(w[o] * gi.loc[T - o, "fga"] for o in offs)
                px.append(num / max(den, 1)); py.append(gi.loc[T + 1, "m_fgpct"])
        rr, n = _r2(np.array(px), np.array(py))
        print(f"({'2' if span=='2yr' else '3'}) {span}-pooled decayed m_fgpct -> next disjoint yr: "
              f"r2={rr:.3f}  (n={n})")

    pt = seas.pivot_table(index="player_id", columns="season_start", values="m_tovp100")
    tx, ty = [], []
    for s in range(FIRST_MATCHUP_SEASON, LAST):
        both = pt[[s, s + 1]].dropna()
        tx += both[s].tolist(); ty += both[s + 1].tolist()
    print(f"(ref) single-season YoY m_tovp100:                r2={_r2(np.array(tx), np.array(ty))[0]:.3f}")
    print("\nVERDICT: m_fgpct stays noise (<0.10) even pooled -> SKIP the containment feature; the "
          "one\nstable feature (m_tovp100) restates steals. Data kept. See CLAUDE.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
