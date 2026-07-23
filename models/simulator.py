"""
Monte Carlo simulation engine for MLB and soccer games.

Uses Poisson run/goal distributions calibrated against The Odds API no-vig
true probabilities and over/under lines to simulate game outcomes at scale.

Supports: baseball_mlb, all soccer_* sport keys.
NFL support is stubbed for future use.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

MLB_SPORT_KEY = "baseball_mlb"
NFL_SPORT_KEY = "americanfootball_nfl"  # future

SOCCER_SPORT_KEYS: frozenset[str] = frozenset({
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_france_ligue_one",
    "soccer_italy_serie_a",
    "soccer_fifa_world_cup",
    "soccer_uefa_nations_league",
    "soccer_conmebol_copa_libertadores",
    "soccer_conmebol_copa_america",
    "soccer_uefa_euro_qualification",
})

SUPPORTED_SPORT_KEYS: frozenset[str] = frozenset({MLB_SPORT_KEY}) | SOCCER_SPORT_KEYS

_DEFAULT_TOTAL: dict[str, float] = {
    MLB_SPORT_KEY: 8.5,
    "soccer": 2.5,
}

N_SIMS_DEFAULT = 10_000
N_CAL = 4_000      # sims used during lambda calibration (fast; reused with fixed seed)
CAL_SEED = 7       # fixed seed for reproducible calibration


@dataclass
class SimResult:
    sport_key: str
    game: str
    home_team: str
    away_team: str
    n_sims: int
    projected_outcome: str   # "Home Win" | "Away Win" | "Draw"
    confidence: float        # 0.0–100.0 — fraction of sims predicting projected_outcome
    home_win_pct: float
    away_win_pct: float
    draw_pct: float
    avg_home_score: float
    avg_away_score: float
    narrative_data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lambda calibration
# ---------------------------------------------------------------------------

def _calibrate_lambdas(home_win_prob: float, total: float) -> tuple[float, float]:
    """
    Binary-search for Poisson lambdas (λ_h, λ_a) where:
      - λ_h + λ_a = total
      - P(Poisson(λ_h) > Poisson(λ_a)) ≈ home_win_prob

    Uses a fixed-seed RNG so calibration is fast and consistent across runs.
    """
    cal_rng = np.random.default_rng(seed=CAL_SEED)
    lo, hi = 0.05, 0.95

    for _ in range(18):  # ~2^18 resolution → sub-0.01 precision on [0,1]
        mid = (lo + hi) / 2.0
        lam_h = total * mid
        lam_a = total * (1.0 - mid)
        h = cal_rng.poisson(lam_h, N_CAL)
        a = cal_rng.poisson(lam_a, N_CAL)
        sim_hw = float(np.sum(h > a)) / N_CAL
        if sim_hw < home_win_prob:
            lo = mid
        else:
            hi = mid

    frac = (lo + hi) / 2.0
    lam_h = max(total * frac, 0.05)
    lam_a = max(total * (1.0 - frac), 0.05)
    return lam_h, lam_a


# ---------------------------------------------------------------------------
# Sport-specific simulators
# ---------------------------------------------------------------------------

def _simulate_mlb(
    home_win_prob: float,
    total_line: Optional[float],
    n_sims: int,
    rng: np.random.Generator,
) -> dict:
    """Poisson run-distribution simulation for MLB."""
    total = total_line if total_line is not None else _DEFAULT_TOTAL[MLB_SPORT_KEY]
    lam_h, lam_a = _calibrate_lambdas(home_win_prob, total)

    home_runs = rng.poisson(lam_h, n_sims)
    away_runs = rng.poisson(lam_a, n_sims)

    # Tied games go to extra innings — resolve with weighted coin reflecting win prob
    ties_mask = home_runs == away_runs
    n_ties = int(np.sum(ties_mask))
    extra_home = int(np.sum(rng.random(n_ties) < home_win_prob)) if n_ties > 0 else 0

    hw = int(np.sum(home_runs > away_runs)) + extra_home
    aw = n_sims - hw

    over_pct = float(np.sum(home_runs + away_runs > total_line) / n_sims * 100) if total_line is not None else None
    under_pct = float(np.sum(home_runs + away_runs < total_line) / n_sims * 100) if total_line is not None else None

    return {
        "home_wins": hw,
        "away_wins": aw,
        "draws": 0,
        "home_win_pct": hw / n_sims * 100,
        "away_win_pct": aw / n_sims * 100,
        "draw_pct": 0.0,
        "avg_home_score": float(np.mean(home_runs)),
        "avg_away_score": float(np.mean(away_runs)),
        "over_pct": over_pct,
        "under_pct": under_pct,
        "lam_home": round(lam_h, 3),
        "lam_away": round(lam_a, 3),
    }


def _simulate_soccer(
    home_win_prob: float,
    draw_prob: float,
    away_win_prob: float,
    total_line: Optional[float],
    n_sims: int,
    rng: np.random.Generator,
) -> dict:
    """Bivariate Poisson goal-distribution simulation for soccer (3-way outcomes)."""
    # Normalize
    s = home_win_prob + draw_prob + away_win_prob
    if s <= 0:
        s = 1.0
    home_win_prob /= s
    draw_prob /= s
    away_win_prob /= s

    total = total_line if total_line is not None else _DEFAULT_TOTAL["soccer"]

    # Calibrate lambdas from the non-draw ratio; the Poisson model handles draws
    # naturally via tied scores.  We calibrate on P(home > away) ignoring draws
    # so the ratio is correct, then the raw draw frequency emerges from Poisson.
    eff_hw = home_win_prob / max(home_win_prob + away_win_prob, 1e-6)
    lam_h, lam_a = _calibrate_lambdas(eff_hw, total)

    home_goals = rng.poisson(lam_h, n_sims)
    away_goals = rng.poisson(lam_a, n_sims)

    hw = int(np.sum(home_goals > away_goals))
    draws = int(np.sum(home_goals == away_goals))
    aw = int(np.sum(away_goals > home_goals))

    over_pct = float(np.sum(home_goals + away_goals > total_line) / n_sims * 100) if total_line is not None else None
    under_pct = float(np.sum(home_goals + away_goals < total_line) / n_sims * 100) if total_line is not None else None

    return {
        "home_wins": hw,
        "draws": draws,
        "away_wins": aw,
        "home_win_pct": hw / n_sims * 100,
        "away_win_pct": aw / n_sims * 100,
        "draw_pct": draws / n_sims * 100,
        "avg_home_score": float(np.mean(home_goals)),
        "avg_away_score": float(np.mean(away_goals)),
        "over_pct": over_pct,
        "under_pct": under_pct,
        "lam_home": round(lam_h, 3),
        "lam_away": round(lam_a, 3),
        "market_draw_prob": round(draw_prob * 100, 1),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_simulation(
    sport_key: str,
    game: str,
    home_team: str,
    away_team: str,
    home_win_prob: float,
    away_win_prob: float,
    draw_prob: float = 0.0,
    total_line: Optional[float] = None,
    n_sims: int = N_SIMS_DEFAULT,
) -> Optional[SimResult]:
    """
    Run a Monte Carlo game simulation.

    Returns a SimResult, or None if the sport is not supported.
    home_win_prob / away_win_prob / draw_prob should be the no-vig true
    probabilities derived from The Odds API sharp-book consensus.
    """
    if sport_key not in SUPPORTED_SPORT_KEYS:
        return None

    rng = np.random.default_rng()  # fresh unseeded RNG for each simulation

    try:
        if sport_key == MLB_SPORT_KEY:
            raw = _simulate_mlb(home_win_prob, total_line, n_sims, rng)
        elif sport_key in SOCCER_SPORT_KEYS:
            raw = _simulate_soccer(home_win_prob, draw_prob, away_win_prob, total_line, n_sims, rng)
        else:
            return None
    except Exception:
        log.exception("Simulation error for %r (%s)", game, sport_key)
        return None

    hw_pct = raw["home_win_pct"]
    aw_pct = raw["away_win_pct"]
    dr_pct = raw["draw_pct"]

    candidates: dict[str, float] = {"Home Win": hw_pct, "Away Win": aw_pct}
    if sport_key in SOCCER_SPORT_KEYS:
        candidates["Draw"] = dr_pct

    projected_outcome = max(candidates, key=candidates.get)
    confidence = candidates[projected_outcome]

    return SimResult(
        sport_key=sport_key,
        game=game,
        home_team=home_team,
        away_team=away_team,
        n_sims=n_sims,
        projected_outcome=projected_outcome,
        confidence=round(confidence, 1),
        home_win_pct=round(hw_pct, 1),
        away_win_pct=round(aw_pct, 1),
        draw_pct=round(dr_pct, 1),
        avg_home_score=round(raw["avg_home_score"], 2),
        avg_away_score=round(raw["avg_away_score"], 2),
        narrative_data={
            **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in raw.items()},
            "total_line": total_line,
            "market_home_win_prob": round(home_win_prob * 100, 1),
            "market_away_win_prob": round(away_win_prob * 100, 1),
            "market_draw_prob": round(draw_prob * 100, 1) if draw_prob else None,
        },
    )
