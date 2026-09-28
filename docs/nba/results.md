# NBA — measured backtest results

> Moved verbatim from `CLAUDE.md` on 2026-09-28 so it is read on demand instead of loaded
> into every session. "Above" / "below" may now point to a sibling file in `docs/nba/` —
> the index is the "Authoritative references" section of `AI_ENGINEERING.md`.

## Measured results so far

Walk-forward means: to predict season N, train only on seasons before N. All errors in
82-game-equivalent wins, lower is better.

**Current shipped model** (roster mode + one-year carryover), 2017-18..2025-26:

| Model / baseline | MAE | Notes |
|---|---|---|
| Market (preseason win totals) | **6.88** | the yardstick; we do not beat it |
| **wincurve — + position-rel *oreb* offense + usage-weighted RAPM offense** | **7.39** | shipped 2026-08-07; two changes: (1) `oreb_p100` REPLACES `ts_pct` in the position-relative offense set (+0.05 full-pipeline, 6/6 folds — the offensive twin of the shipped defensive `dreb_p100`, where `ts_pct` had become a null once the RAPM offense blend shipped); (2) the RAPM offense blend weight switches turnover → prior top-scorer share (aggregate-neutral, restores stable-roster stars). Net within noise of 7.40, driven by oreb |
| wincurve — + RAPM offensive blend (turnover weight) | 7.40 | prior (2026-08-06); +0.11 to +0.15 (7.52 → 7.39–7.40 paired-seed, ±0.11–0.17 SE, 5-7/9 folds, every weight variant improved) — extends the defensive RAPM blend to offense; guard gravity/creation is what plus-minus sees and the box does not |
| wincurve — + position-relative offensive standardization (ts_pct) | 7.58 | prior (2026-08-06), SUPERSEDED by oreb; +0.06 pure-box (ts_pct alone, ±0.065 SE, 3/6 folds) but a null in the full pipeline (the RAPM offense blend already corrects its efficiency confound) — replaced by oreb, which removes the orthogonal rebounding-role confound |
| wincurve — + RAPM prior re-anchored on current box defense | 7.58 | prior; +0.036 (7.628 → 7.591 paired-seed, ±0.017 SE, 5/6 folds) — the RAPM prior was stale (pre position/tracking fixes) |
| wincurve — + BPM position fallback (pre-2013-14) | 7.61 | prior; +0.011 (7.622 → 7.612, ±0.0039 SE, 5/6 folds) — fixes position-relative rebounding's silent no-op on 8 of 21 backbone seasons |
| wincurve — + position-relative defensive rebounding | 7.62 | prior; +0.11 (7.74 → 7.62, ±0.055 SE, 6/9 folds) — first defensive change to help accuracy AND credibility |
| wincurve — + RAPM def-blend + rim/hustle tracking def | 7.74 | prior; tracking step within noise (7.77 → 7.74) — kept for per-player credibility |
| wincurve — roster + carryover + RAPM def-blend | 7.77 | before the tracking-defense features |
| wincurve — roster mode + carryover (box def only) | 7.95 | the shipped model before the RAPM blend |
| Leaky upper bound (actual roster + minutes) | 7.11 | ceiling given perfect roster knowledge |
| Mean-reverted previous wins ← *the gate* | 8.13 | every stage must clear this |
| Previous wins (persistence) | 8.75 | |
| Always .500 | 10.18 | |
| *Theoretical binomial noise floor* | *3.44* | not achievable (see below) |

Longer-window baseline figures (2013-14..2025-26): market 6.67 excl. 2019-20, naive 8.07.
Fitted mean-reversion coefficient **k ≈ 0.62**, stable across folds: teams retain ~62% of
their distance from .500 year over year. Implied true-talent spread is **SD ≈ 11.6 wins**
vs **4.3 wins** of luck, so talent variation dwarfs randomness and projection is worth doing.

**The honest read:** we do not beat the market on aggregate accuracy, and should not expect
to. The deliverable is per-team *disagreement* with an attached explanation, plus calibrated
intervals that widen for genuinely uncertain (high-turnover) teams.

### ⚠️ How to read the noise floor honestly

The 3.44 MAE binomial floor is **not an achievable target**. It assumes we know each
team's true strength in October *and* that strength stays constant all season. Real
rosters change through unforeseeable injuries and trades, so the practically achievable
frontier is considerably higher — and the market's 6.67 is probably close to it.

Do **not** present "market captures only 30% of the gap to the floor" as an opportunity
estimate. Realistic aggregate improvement over the market is on the order of a few
tenths to ~1 win of MAE, and may be zero. The genuine value of this project is
per-team disagreement attribution, not beating the market on average.

---
