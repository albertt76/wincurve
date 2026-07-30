# wincurve

Bottom-up projections of every NBA team's regular-season record — as a **distribution**,
not a single number. Each team's *win curve* widens or tightens with how much its roster
turned over, and the intervals are calibrated (a nominal 80% range contained the actual
result 79.6% of the time in backtesting).

The point isn't a point estimate. It's to see, per team, where an analytically-driven model
disagrees with the betting/prediction market — and to be able to say *why*.

## How it works

```
player impact per 100 poss   -> aging curves + shrinkage      (talent, from history only)
      + minutes & availability -> 240-min/game budget          (who actually plays)
      = team rating            -> minute-weighted aggregation   (near parameter-free)
      + one-year carryover     -> last season's residual        (error persists ~1 season)
      -> Monte Carlo season    -> real schedule, home court     (win DISTRIBUTION per team)
      -> market comparison      (strictly downstream, never a feature)
```

Everything is **walk-forward**: to project a season, only earlier seasons are used. The bar
each stage must clear is mean-reverted previous wins; the yardstick is the market's preseason
win totals.

## Where it stands

| | MAE (avg miss, wins) |
|---|---|
| Market (preseason win totals) | 6.88 |
| **wincurve** (roster mode + carryover) | **7.95** |
| Mean-reverted previous wins (baseline) | 8.13 |

It does **not** beat the market on aggregate accuracy — season win totals are a sharp,
heavily-bet market near the irreducible noise floor. The value is per-team disagreement
with an explanation attached, and a calibrated sense of which teams are genuinely uncertain.

## Layout

- `nbaproj/` — the model: `impact` (player value), `aging`, `availability`, `minutes`,
  `project` (team aggregation), `carryover`, `simulate` (Monte Carlo), `odds` (market),
  `rapm`/`pbp` (defensive plus-minus, in progress).
- `scripts/` — data pulls and staged backtest reports.
- `ui/` — the interactive win-curve board with a roster what-if editor.
- `CLAUDE.md` — decisions, measured results, and the landmine list.
- `DESIGN.md` — architecture and the statistical traps that shaped it.

Data (nba_api, gitignored) regenerates with `python scripts/fetch_all.py`.
