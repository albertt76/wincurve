"""Bulk play-by-play from shufinskiy/nba_data, and lineup reconstruction from it.

## Why bulk instead of the live endpoint

stats.nba.com rate-limits GameRotation hard -- a full-season RAPM pull stalled at 350/1230
games. The shufinskiy/nba_data repo mirrors the same stats.nba.com play-by-play as static,
Apache-2.0 files on GitHub: no API key, no account, no rate limit, all seasons from 1996-97.
One ~8.5 MB download per season replaces ~2,500 throttled live calls.

The bulk feed is also *better* structured for our purpose than the live v3 endpoint: a
substitution row carries BOTH player ids explicitly (PLAYER1 = out, PLAYER2 = in), where
the live v3 gave only the outgoing id and the incoming player by name.

## Lineup reconstruction (the part that needs care)

Play-by-play alone does not state who is on the floor -- only substitutions. Players enter at
period boundaries with no SUB event, which is the failure that gave ~8 minutes of error per
game on the first live attempt. The fix here is to derive each period's starters from the
events themselves: a player who records an event or is substituted OUT before being
substituted IN during a period must have started that period. This is validated against the
GameRotation stints (exact, 0.000 min vs box score) that we already cached.
"""

from __future__ import annotations

import io
import logging
import tarfile

import numpy as np
import pandas as pd

from .cache import DATA_DIR

log = logging.getLogger(__name__)

MANIFEST_URL = "https://raw.githubusercontent.com/shufinskiy/nba_data/main/list_data.txt"
BULK_DIR = DATA_DIR / "raw" / "bulk_pbp"

SUB_EVENT = 8  # EVENTMSGTYPE for a substitution


def _manifest() -> dict[str, str]:
    """Map dataset name -> download URL from the repo's manifest file."""
    import urllib.request
    with urllib.request.urlopen(MANIFEST_URL, timeout=45) as resp:
        text = resp.read().decode("utf-8")
    out = {}
    for line in text.splitlines():
        if "=" in line:
            name, url = line.split("=", 1)
            out[name.strip()] = url.strip()
    return out


def download_season(season_start: int, *, refresh: bool = False) -> pd.DataFrame:
    """Download and cache one season's stats.nba.com play-by-play as a DataFrame.

    Cached as parquet after the first download. `season_start` is the year the season
    begins (2024 -> 2024-25), matching the rest of the codebase.
    """
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    parquet = BULK_DIR / f"nbastats_{season_start}.parquet"
    if parquet.exists() and not refresh:
        return pd.read_parquet(parquet)

    import urllib.request
    url = _manifest().get(f"nbastats_{season_start}")
    if not url:
        raise ValueError(f"no nbastats dataset for {season_start} in manifest")

    log.info("downloading bulk PBP for %d-%02d ...", season_start, (season_start + 1) % 100)
    with urllib.request.urlopen(url, timeout=180) as resp:
        raw = resp.read()
    # .tar.xz containing a single CSV.
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:xz") as tar:
        member = next(m for m in tar.getmembers() if m.name.endswith(".csv"))
        df = pd.read_csv(tar.extractfile(member), low_memory=False)

    df["GAME_ID"] = df["GAME_ID"].astype(str).str.zfill(10)
    df.to_parquet(parquet, index=False)
    log.info("cached %d rows -> %s", len(df), parquet)
    return df


def _period_elapsed(period: int, pctime: str) -> float:
    """Per-period clock 'MM:SS' remaining -> seconds elapsed since game start."""
    try:
        mm, ss = str(pctime).split(":")
        rem = int(mm) * 60 + int(ss)
    except (ValueError, AttributeError):
        rem = 0
    plen = 720 if period <= 4 else 300
    prior = (period - 1) * 720 if period <= 4 else 2880 + (period - 5) * 300
    return prior + (plen - rem)


def _home_away(g: pd.DataFrame) -> tuple[int, int]:
    """Home and away team ids, from which side's description carries each team's events."""
    home = g.loc[g["HOMEDESCRIPTION"].notna() & g["PLAYER1_TEAM_ID"].notna(),
                 "PLAYER1_TEAM_ID"]
    away = g.loc[g["VISITORDESCRIPTION"].notna() & g["PLAYER1_TEAM_ID"].notna(),
                 "PLAYER1_TEAM_ID"]
    home_id = int(home.mode().iloc[0]) if not home.empty else 0
    away_id = int(away.mode().iloc[0]) if not away.empty else 0
    return home_id, away_id


def _period_starters(period_events: pd.DataFrame, team_id: int) -> list[int]:
    """Players who started this period for `team_id`, earliest-appearing first.

    A player started the period if his first involvement in it is anything other than being
    substituted in: he records an event, or is substituted OUT before being substituted IN.
    Even a starter who is never named at tip is recovered this way, because he must act or
    be subbed out eventually.

    The result is ordered by the event number of that first involvement, and the caller
    keeps the earliest five. That ordering matters: substitution/team-attribution quirks
    occasionally flag a sixth "starter" who was really subbed in, and he always appears
    *later* than the genuine five -- so keeping the earliest five drops exactly him. (An
    unordered set truncation would drop an arbitrary player and corrupt a whole period.)
    """
    subbed_in: set[int] = set()
    first_seen: dict[int, int] = {}

    def note(pid: int, event_num: int) -> None:
        if pid not in subbed_in and pid not in first_seen:
            first_seen[pid] = event_num

    for _, e in period_events.iterrows():
        en = int(e["EVENTNUM"])
        if e["EVENTMSGTYPE"] == SUB_EVENT:
            # A substitution is always within one team; PLAYER1_TEAM_ID (the outgoing
            # player) is reliably populated, PLAYER2_TEAM_ID sometimes is not. Gate both
            # the out and the in on the outgoing player's team.
            if _belongs(e, team_id, "PLAYER1_TEAM_ID"):
                note(int(e["PLAYER1_ID"]), en)          # subbed out => was on floor
                subbed_in.add(int(e["PLAYER2_ID"]))     # subbed in => not a starter
        else:
            for pcol, tcol in (("PLAYER1_ID", "PLAYER1_TEAM_ID"),
                               ("PLAYER2_ID", "PLAYER2_TEAM_ID"),
                               ("PLAYER3_ID", "PLAYER3_TEAM_ID")):
                pid = e[pcol]
                if pd.notna(pid) and pid != 0 and _belongs(e, team_id, tcol):
                    note(int(pid), en)

    return [p for p, _ in sorted(first_seen.items(), key=lambda kv: kv[1])]


def _belongs(event: pd.Series, team_id: int, team_col: str) -> bool:
    return pd.notna(event[team_col]) and int(event[team_col]) == team_id


def build_segments_bulk(game_pbp: pd.DataFrame) -> pd.DataFrame:
    """Reconstruct constant-lineup segments for one game from bulk play-by-play.

    Same schema as nbaproj.pbp.build_segments: game_id, home_id, away_id, start_s, end_s,
    dur_s, home_margin, poss, home_p1..5, away_p1..5. Built entirely offline from the bulk
    feed -- no live API calls.
    """
    g = game_pbp.sort_values(["PERIOD", "EVENTNUM"]).reset_index(drop=True)
    if g.empty:
        return pd.DataFrame()
    game_id = str(g["GAME_ID"].iloc[0])
    home_id, away_id = _home_away(g)
    if home_id == 0 or away_id == 0:
        return pd.DataFrame()

    g["t"] = [_period_elapsed(p, c) for p, c in zip(g["PERIOD"], g["PCTIMESTRING"])]
    score = _score_series(g)

    on = {home_id: set(), away_id: set()}
    rows: list[dict] = []
    seg_start = 0.0
    seg_home = seg_away = None

    for period in sorted(g["PERIOD"].unique()):
        pe = g[g["PERIOD"] == period]
        # Seed the period's on-court five for each team.
        for tid in (home_id, away_id):
            starters = _period_starters(pe, tid)
            on[tid] = set(list(starters)[:5])
        seg_start = pe["t"].iloc[0]
        seg_home, seg_away = _snapshot(on, home_id, away_id)

        for _, e in pe.iterrows():
            if e["EVENTMSGTYPE"] != SUB_EVENT:
                continue
            t = e["t"]
            # Close the running segment at the sub time.
            if seg_home is not None and len(seg_home) == 5 and len(seg_away) == 5 \
                    and t > seg_start:
                rows.append(_segment_row(game_id, home_id, away_id, seg_start, t,
                                         seg_home, seg_away, score))
            # Apply the sub to the outgoing player's team -- both players belong to it,
            # and PLAYER1_TEAM_ID is the reliably-populated side.
            out_id, in_id = int(e["PLAYER1_ID"]), int(e["PLAYER2_ID"])
            sub_team = int(e["PLAYER1_TEAM_ID"]) if pd.notna(e["PLAYER1_TEAM_ID"]) else None
            if sub_team in on:
                on[sub_team].discard(out_id)
                on[sub_team].add(in_id)
            seg_start = t
            seg_home, seg_away = _snapshot(on, home_id, away_id)

        # Close the final segment of the period.
        t_end = pe["t"].iloc[-1]
        if seg_home is not None and len(seg_home) == 5 and len(seg_away) == 5 \
                and t_end > seg_start:
            rows.append(_segment_row(game_id, home_id, away_id, seg_start, t_end,
                                     seg_home, seg_away, score))

    return pd.DataFrame(rows)


def _snapshot(on: dict[int, set[int]], home_id: int, away_id: int):
    return sorted(on[home_id]), sorted(on[away_id])


def _segment_row(game_id, home_id, away_id, t0, t1, home, away, score):
    hs0, as0 = _score_at(score, t0)
    hs1, as1 = _score_at(score, t1)
    # Store each team's points, not just the margin: RAPM needs real offensive points per
    # side (see nbaproj.rapm.build_design). Margin alone forces a degraded, home/away-sign-
    # sensitive fallback that scrambles the estimate.
    rec = {"game_id": game_id, "home_id": home_id, "away_id": away_id,
           "start_s": t0, "end_s": t1, "dur_s": t1 - t0,
           "home_pts": hs1 - hs0, "away_pts": as1 - as0,
           "home_margin": (hs1 - hs0) - (as1 - as0), "poss": (t1 - t0) / 28.8}
    for i, p in enumerate(home[:5]):
        rec[f"home_p{i + 1}"] = p
    for i, p in enumerate(away[:5]):
        rec[f"away_p{i + 1}"] = p
    return rec


def _score_series(g: pd.DataFrame) -> pd.DataFrame:
    """(elapsed, home, away) from the SCORE column, forward-filled.

    The stats.nba.com SCORE column is 'AWAY - HOME' (visitor first) -- verified against
    final scores. Parsing it as home-first swaps every segment's point attribution and
    scrambles the offense/defense split in RAPM, so the order here is deliberate.
    """
    sc = g[["t", "SCORE"]].dropna(subset=["SCORE"]).copy()
    parts = sc["SCORE"].astype(str).str.split(" - ", expand=True)
    sc["away"] = pd.to_numeric(parts[0], errors="coerce")
    sc["home"] = pd.to_numeric(parts[1], errors="coerce")
    return sc.dropna(subset=["home", "away"]).sort_values("t").reset_index(drop=True)


def _score_at(score: pd.DataFrame, t: float) -> tuple[float, float]:
    if score.empty:
        return 0.0, 0.0
    prior = score[score["t"] <= t]
    if prior.empty:
        return 0.0, 0.0
    row = prior.iloc[-1]
    return float(row["home"]), float(row["away"])


def segments_for_season(season_start: int, *, refresh: bool = False) -> pd.DataFrame:
    """All constant-lineup segments for a season, from bulk play-by-play. Cached.

    Fully offline after the one bulk download -- no per-game API calls, no rate limiting,
    so an entire season builds in a couple of minutes and any season back to 1996-97 is
    reachable. Validated RAPM-equivalent to exact GameRotation stints (corr 0.99).
    """
    out = DATA_DIR / "processed" / f"segments_bulk_{season_start}.parquet"
    if out.exists() and not refresh:
        return pd.read_parquet(out)
    pbp = download_season(season_start)
    frames = [build_segments_bulk(pbp[pbp["GAME_ID"] == g])
              for g in pbp["GAME_ID"].unique()]
    frames = [f for f in frames if not f.empty]
    seg = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out.parent.mkdir(parents=True, exist_ok=True)
    seg.to_parquet(out, index=False)
    log.info("season %d: %d segments from %d games", season_start,
             len(seg), pbp["GAME_ID"].nunique())
    return seg
