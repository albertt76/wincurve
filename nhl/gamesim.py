"""Season simulation (Stage 5): turn team goal rates into a standings-POINTS distribution.

Layer F of the architecture. The projection (Stages 3-4) gives each team a strength; Stages 4-5
turn that strength into a projected **goals-for / goals-against per game**. This module runs the
games: model each game's goals, branch regulation / overtime / shootout, award the 2 / 1 / 0
standings points (the "loser point" for an OT/SO loss is what makes hockey points a *trinomial*,
not a coin flip), and sum a full 82-game schedule many times to get each team's **points
distribution** (mean + calibrated interval), not just a point estimate.

Why the pieces are shaped the way they are (all measured on 2010-11..2025-26 `team_summary`):

- **Goals ~ Poisson, but the OT rate is set empirically.** Two independent Poisson goal counts
  under-predict ties (~0.18 vs the real ~0.23 of games going past regulation), because hockey has
  score effects (a leading team sits back, a trailing team pulls its goalie) that compress margins.
  So we use the Poisson model only for *which team is better* (the conditional regulation win
  probability ``w_reg``) and take the share of games going to OT/SO from the data (``OT_RATE``).
- **Overtime/shootout is nearly a coin flip.** A .70-point% team wins only ~55% of its
  past-regulation games (a .40 team ~44%): slope ~0.38 in point% space. So the OT/SO winner is the
  regulation edge shrunk hard toward 0.5 (``OT_SKILL``).
- **Two sources of spread.** Given fixed talent, a team's 82-game points still swing from luck
  (the binomial season draw, SD ~8-9 points). On top of that our *talent estimate* is itself wrong
  (projection error, the dominant term). The predictive interval convolves both: the sim's luck
  distribution plus a points-space projection-error term ``sigma_extra`` fit walk-forward to hit
  nominal coverage (the NBA project's ``fit_rating_sigma`` move, adapted).

Everything is per-fold walk-forward safe: this module is pure mechanics (probabilities in, sampled
points out); the calibration of strength->goal-rates and of ``sigma_extra`` lives in the report.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import poisson

# League anchors (full-season pooled 2010-11..2025-26; the report refits/updates these per fold).
LEAGUE_GF = 2.89        # league mean goals-for per team per game (drifts 2.66 -> 3.08; use prior yr)
OT_RATE = 0.23          # share of games decided past regulation (OT or shootout); stable ~0.21-0.25
OT_SKILL = 0.45         # OT/SO winner prob = 0.5 + OT_SKILL*(w_reg - 0.5); OT is close to a coin flip
GAMES = 82              # a full season
MAX_GOALS = 16          # truncation for the Poisson win-probability sum (P(>16 goals) ~ 0)


def reg_win_prob(lam_i: np.ndarray, lam_j: np.ndarray) -> np.ndarray:
    """P(team i outscores team j | the game is decided in regulation), under independent Poisson.

    Vectorized over teams. Returns the *conditional* win probability ``P(i>j)/(P(i>j)+P(i<j))`` --
    the raw Poisson tie mass is discarded because the OT rate is supplied empirically by the caller.
    """
    lam_i = np.asarray(lam_i, dtype=float)[:, None]     # (T, 1)
    lam_j = np.asarray(lam_j, dtype=float)[:, None]
    k = np.arange(MAX_GOALS + 1)[None, :]               # (1, G)
    pi = poisson.pmf(k, lam_i)                          # (T, G) i scores exactly k
    cj = poisson.cdf(k, lam_j)                          # (T, G) j scores <= k
    p_i_gt_j = (pi[:, 1:] * cj[:, :-1]).sum(axis=1)     # sum_a P(i=a) P(j<=a-1)
    p_tie = (pi * poisson.pmf(k, lam_j)).sum(axis=1)
    p_i_lt_j = 1.0 - p_i_gt_j - p_tie
    denom = p_i_gt_j + p_i_lt_j
    return np.where(denom > 0, p_i_gt_j / denom, 0.5)


def game_point_probs(gf: np.ndarray, ga: np.ndarray, *, league_gf: float = LEAGUE_GF,
                     ot_rate: float = OT_RATE, ot_skill: float = OT_SKILL,
                     opp_gf: np.ndarray | None = None, opp_ga: np.ndarray | None = None
                     ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-game (P(win, 2pts), P(OT/SO loss, 1pt), P(reg loss, 0pts)) for each team.

    ``gf`` / ``ga`` are each team's expected goals for / against per game **vs a league-average
    opponent** (a balanced-schedule assumption -- every team's 82 opponents treated as average).
    A team's scoring rate against a specific opponent combines the two multiplicatively:
    ``lambda_i = gf_i * ga_opp / league_gf`` (if the opponent is average, ``ga_opp = league_gf`` and
    ``lambda_i = gf_i``). Pass ``opp_gf`` / ``opp_ga`` for a real opponent instead of the average.
    """
    gf = np.asarray(gf, dtype=float)
    ga = np.asarray(ga, dtype=float)
    og = league_gf if opp_gf is None else np.asarray(opp_gf, dtype=float)     # opponent offense
    oa = league_gf if opp_ga is None else np.asarray(opp_ga, dtype=float)     # opponent defense
    lam_i = gf * oa / league_gf          # i's goals: own offense scaled by opp defense
    lam_j = og * ga / league_gf          # opponent's goals: opp offense scaled by i's defense
    w_reg = reg_win_prob(lam_i, lam_j)
    p_ot_win = 0.5 + ot_skill * (w_reg - 0.5)
    p_win = (1.0 - ot_rate) * w_reg + ot_rate * p_ot_win        # 2 points (reg or OT/SO win)
    p_otl = ot_rate * (1.0 - p_ot_win)                          # 1 point (OT/SO loss)
    p_loss = (1.0 - ot_rate) * (1.0 - w_reg)                    # 0 points
    return p_win, p_otl, p_loss


def expected_points(gf: np.ndarray, ga: np.ndarray, *, games: int = GAMES, **kw) -> np.ndarray:
    """Closed-form expected 82-game points per team (the sim mean; no Monte Carlo needed)."""
    p_win, p_otl, _ = game_point_probs(gf, ga, **kw)
    return games * (2.0 * p_win + p_otl)


def expected_wins(gf: np.ndarray, ga: np.ndarray, *, games: int = GAMES, **kw) -> np.ndarray:
    """Closed-form expected 82-game WINS per team (regulation + OT + shootout; the standings W).

    Standings points and wins are different targets in hockey -- a win is 2 points but an OT/SO loss
    is still 1 -- so a wins-settled market (Kalshi KXNHLWINS) is compared to this, not to points."""
    p_win, _, _ = game_point_probs(gf, ga, **kw)
    return games * p_win


def simulate_points(gf: np.ndarray, ga: np.ndarray, *, games: int = GAMES, n_sims: int = 20000,
                    sigma_extra: float = 0.0, rng: np.random.Generator | None = None,
                    **kw) -> np.ndarray:
    """Monte-Carlo season points: returns an ``(n_sims, n_teams)`` array of sampled 82-game points.

    Each sim draws a team's win / OT-loss / regulation-loss counts from the per-game probabilities
    (the season *luck* spread, given fixed talent), then adds a Normal ``sigma_extra`` points offset
    representing our *talent-estimate* error (fit walk-forward by the report to calibrate coverage).
    The two convolved give the predictive distribution. Balanced schedule: every game uses the same
    per-team probabilities, so the season is ``games`` i.i.d. trinomial draws (sampled as two nested
    binomials -- wins, then OT-losses among the rest).
    """
    rng = np.random.default_rng() if rng is None else rng
    p_win, p_otl, p_loss = game_point_probs(gf, ga, **kw)
    T = len(p_win)
    p_win = np.broadcast_to(p_win, (n_sims, T))
    otl_given_nonwin = np.where((p_otl + p_loss) > 0, p_otl / (p_otl + p_loss), 0.0)
    otl_given_nonwin = np.broadcast_to(otl_given_nonwin, (n_sims, T))
    wins = rng.binomial(games, p_win)                       # (n_sims, T)
    otls = rng.binomial(games - wins, otl_given_nonwin)
    pts = 2.0 * wins + otls
    if sigma_extra > 0:
        pts = pts + rng.normal(0.0, sigma_extra, size=pts.shape)
    return np.clip(pts, 0.0, 2.0 * games)


def summarize(samples: np.ndarray, quantiles=(0.1, 0.5, 0.9)) -> dict:
    """Per-team mean / SD / requested quantiles from an ``(n_sims, n_teams)`` sample array."""
    out = {"mean": samples.mean(axis=0), "sd": samples.std(axis=0)}
    for q in quantiles:
        out[f"p{int(round(q * 100))}"] = np.quantile(samples, q, axis=0)
    return out
