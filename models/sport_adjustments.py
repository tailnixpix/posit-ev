"""
sport_adjustments.py — Sport-specific EV model adjustments.

Each adjustment is a toggleable boolean flag in ADJUSTMENT_CONFIG.
Adjustments modify the true_prob estimate before EV is calculated,
or apply a confidence penalty / multiplier to the final EV%.

Supported sports:
  - NHL  : goalie confirmation, puck line handling
  - NBA  : back-to-back rest penalty, home/away split weighting
  - Soccer (EPL / La Liga / Bundesliga / MLS):
            3-way 1X2 handling, draw no-bet, European fatigue
"""

from __future__ import annotations

import sys
import os
import logging
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models.no_vig import decimal_to_american

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global adjustment config — set any flag to False to disable that logic
# ---------------------------------------------------------------------------

ADJUSTMENT_CONFIG: dict = {
    # --- NHL ---
    "nhl_goalie_confirmation":    True,   # flag games w/ unconfirmed starters
    "nhl_puck_line_ev_separate":  True,   # treat puck line (-1.5) as distinct from ATS spreads

    # --- NBA ---
    "nba_rest_advantage":         True,   # penalise B2B team true_prob
    "nba_home_away_split":        True,   # weight home/away historical splits into prob

    # --- Soccer ---
    "soccer_three_way_1x2":       True,   # handle home/draw/away separately
    "soccer_draw_no_bet":         True,   # compute DNB implied odds from 1X2
    "soccer_euro_fatigue":        True,   # discount midweek European competition teams
}

# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

@dataclass
class GameContext:
    """
    Metadata about a game used to apply adjustments.
    All fields are optional — populate only what is available.
    """
    game_id: str = ""
    sport_key: str = ""
    home_team: str = ""
    away_team: str = ""
    commence_time: Optional[pd.Timestamp] = None

    # NHL
    home_goalie_confirmed: Optional[bool] = None
    away_goalie_confirmed: Optional[bool] = None

    # NBA
    home_b2b: bool = False       # home team playing second night of back-to-back
    away_b2b: bool = False       # away team playing second night of back-to-back
    home_win_pct_home: Optional[float] = None   # home team win% at home this season
    away_win_pct_away: Optional[float] = None   # away team win% on road this season

    # Soccer
    home_euro_midweek: bool = False   # home played Champions/Europa League in last 4 days
    away_euro_midweek: bool = False

    # Derived flags (populated by adjustments)
    flags: dict = field(default_factory=dict)


@dataclass
class AdjustedProb:
    """Result of applying adjustments to a single outcome probability."""
    original_prob: float
    adjusted_prob: float
    confidence_multiplier: float = 1.0   # applied to EV% after adjustment
    flags: list = field(default_factory=list)   # human-readable list of applied adjustments
    warnings: list = field(default_factory=list)

    @property
    def effective_prob(self) -> float:
        """Clamp adjusted_prob to a valid probability range."""
        return max(0.001, min(0.999, self.adjusted_prob))


# ---------------------------------------------------------------------------
# NHL adjustments
# ---------------------------------------------------------------------------

# Empirical: B2B / tired goalie situations reduce a team's win probability.
# These are conservative multipliers; calibrate with historical data.
_NHL_UNCONFIRMED_GOALIE_CONFIDENCE_PENALTY = 0.80   # reduce EV confidence by 20%
_NHL_PUCK_LINE_VIG_PREMIUM = 0.02                    # puck lines carry extra vig vs ML


def nhl_goalie_adjustment(
    ctx: GameContext,
    home_true_prob: float,
    away_true_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> tuple[AdjustedProb, AdjustedProb]:
    """
    Flag games where starting goalies are unconfirmed and reduce model
    confidence (not the probability itself, since we don't know which
    backup will start).

    Parameters
    ----------
    ctx : GameContext
    home_true_prob, away_true_prob : float
        No-vig moneyline probabilities.
    config : dict

    Returns
    -------
    (AdjustedProb for home, AdjustedProb for away)

    Notes
    -----
    Goalie confirmation typically arrives 90 min before puck drop.
    Until confirmed, any ML bet carries extra variance — reflected
    as a confidence multiplier reduction rather than a prob shift.
    """
    home_adj = AdjustedProb(original_prob=home_true_prob, adjusted_prob=home_true_prob)
    away_adj = AdjustedProb(original_prob=away_true_prob, adjusted_prob=away_true_prob)

    if not config.get("nhl_goalie_confirmation"):
        return home_adj, away_adj

    if ctx.home_goalie_confirmed is False:
        home_adj.confidence_multiplier *= _NHL_UNCONFIRMED_GOALIE_CONFIDENCE_PENALTY
        home_adj.warnings.append("HOME goalie unconfirmed — EV confidence reduced 20%")
        away_adj.confidence_multiplier *= _NHL_UNCONFIRMED_GOALIE_CONFIDENCE_PENALTY
        away_adj.warnings.append("HOME goalie unconfirmed — affects both sides")

    if ctx.away_goalie_confirmed is False:
        away_adj.confidence_multiplier *= _NHL_UNCONFIRMED_GOALIE_CONFIDENCE_PENALTY
        away_adj.warnings.append("AWAY goalie unconfirmed — EV confidence reduced 20%")
        home_adj.confidence_multiplier *= _NHL_UNCONFIRMED_GOALIE_CONFIDENCE_PENALTY
        home_adj.warnings.append("AWAY goalie unconfirmed — affects both sides")

    if ctx.home_goalie_confirmed is None or ctx.away_goalie_confirmed is None:
        for adj in (home_adj, away_adj):
            adj.warnings.append("Goalie confirmation status unknown — treat EV with caution")

    return home_adj, away_adj


def nhl_puck_line_ev(
    ml_true_prob: float,
    puck_line_american_odds: int,
    is_favorite: bool,
    config: dict = ADJUSTMENT_CONFIG,
) -> dict:
    """
    Puck line (-1.5 for favorites, +1.5 for dogs) requires a probability
    adjustment because winning by 2+ is strictly harder than winning outright.

    Rough empirical mapping:
      Favorite ML prob → puck line win prob ≈ ML_prob * PUCK_LINE_COVER_RATE
      Underdog +1.5 win prob ≈ ML_prob + (1 - ML_prob) * LOSS_BY_ONE_RATE

    These rates should be calibrated on historical NHL data.

    Parameters
    ----------
    ml_true_prob : float
        True (no-vig) moneyline probability for this team.
    puck_line_american_odds : int
        The bookmaker's puck line odds for this outcome.
    is_favorite : bool
        True if evaluating the -1.5 side; False for +1.5.

    Returns
    -------
    dict with adjusted_prob, ev_pct, and flag notes.
    """
    PUCK_LINE_COVER_RATE = 0.72   # favorites who win cover -1.5 ~72% of those wins
    LOSS_BY_ONE_RATE = 0.23       # underdogs who lose, lose by exactly 1 ~23% of the time

    if not config.get("nhl_puck_line_ev_separate"):
        return {"adjusted_prob": ml_true_prob, "note": "puck_line_ev_separate disabled"}

    if is_favorite:
        adjusted = ml_true_prob * PUCK_LINE_COVER_RATE
        note = f"Favorite -1.5: ML prob {ml_true_prob:.3f} → puck line prob {adjusted:.3f} (cover rate {PUCK_LINE_COVER_RATE})"
    else:
        adjusted = ml_true_prob + (1 - ml_true_prob) * LOSS_BY_ONE_RATE
        note = f"Underdog +1.5: ML prob {ml_true_prob:.3f} → puck line prob {adjusted:.3f} (loss-by-1 rate {LOSS_BY_ONE_RATE})"

    return {
        "adjusted_prob": round(adjusted, 4),
        "note": note,
        "flag": "PUCK_LINE_ADJUSTED",
    }


def apply_nhl_adjustments(
    ctx: GameContext,
    home_prob: float,
    away_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> tuple[AdjustedProb, AdjustedProb]:
    """Apply all enabled NHL adjustments and return adjusted probs."""
    home_adj, away_adj = nhl_goalie_adjustment(ctx, home_prob, away_prob, config)

    if home_adj.warnings or away_adj.warnings:
        home_adj.flags.append("NHL_GOALIE_RISK")
        away_adj.flags.append("NHL_GOALIE_RISK")

    return home_adj, away_adj


# ---------------------------------------------------------------------------
# NBA adjustments
# ---------------------------------------------------------------------------

# Empirical B2B penalty: teams on the second night of a B2B historically
# perform ~3-5% worse in win probability terms. Adjust conservatively.
_NBA_B2B_PROB_PENALTY = 0.035       # subtract from B2B team's true_prob
_NBA_HOME_SPLIT_WEIGHT = 0.25       # blend 25% historical home/away split into base prob


def nba_rest_adjustment(
    ctx: GameContext,
    home_prob: float,
    away_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> tuple[AdjustedProb, AdjustedProb]:
    """
    Penalise teams on the second night of a back-to-back.

    The penalty is redistributed to the rested opponent so probabilities
    remain normalised (sum to 1.0 for a 2-way market).

    Parameters
    ----------
    ctx : GameContext
        home_b2b / away_b2b must be populated.

    Returns
    -------
    (AdjustedProb for home, AdjustedProb for away)
    """
    home_adj = AdjustedProb(original_prob=home_prob, adjusted_prob=home_prob)
    away_adj = AdjustedProb(original_prob=away_prob, adjusted_prob=away_prob)

    if not config.get("nba_rest_advantage"):
        return home_adj, away_adj

    delta = 0.0

    if ctx.home_b2b and not ctx.away_b2b:
        delta = _NBA_B2B_PROB_PENALTY
        home_adj.adjusted_prob -= delta
        away_adj.adjusted_prob += delta
        home_adj.flags.append(f"HOME_B2B_PENALTY(-{delta})")
        away_adj.flags.append(f"AWAY_REST_ADVANTAGE(+{delta})")

    elif ctx.away_b2b and not ctx.home_b2b:
        delta = _NBA_B2B_PROB_PENALTY
        away_adj.adjusted_prob -= delta
        home_adj.adjusted_prob += delta
        away_adj.flags.append(f"AWAY_B2B_PENALTY(-{delta})")
        home_adj.flags.append(f"HOME_REST_ADVANTAGE(+{delta})")

    elif ctx.home_b2b and ctx.away_b2b:
        home_adj.flags.append("BOTH_B2B — rest advantage cancels out")
        away_adj.flags.append("BOTH_B2B — rest advantage cancels out")

    return home_adj, away_adj


def nba_home_away_split_adjustment(
    ctx: GameContext,
    home_adj: AdjustedProb,
    away_adj: AdjustedProb,
    config: dict = ADJUSTMENT_CONFIG,
) -> tuple[AdjustedProb, AdjustedProb]:
    """
    Blend in each team's historical home/away win percentage as a
    partial prior, weighted by _NBA_HOME_SPLIT_WEIGHT.

    blended_home = (1 - w) * model_home_prob + w * home_win_pct_at_home
    blended_away = 1 - blended_home

    Parameters
    ----------
    ctx : GameContext
        home_win_pct_home / away_win_pct_away must be populated.
    home_adj, away_adj : AdjustedProb
        Pass the output of nba_rest_adjustment (or raw probs).
    """
    if not config.get("nba_home_away_split"):
        return home_adj, away_adj

    w = _NBA_HOME_SPLIT_WEIGHT
    base_home = home_adj.adjusted_prob

    if ctx.home_win_pct_home is not None:
        blended = (1 - w) * base_home + w * ctx.home_win_pct_home
        home_adj.flags.append(
            f"HOME_SPLIT_BLEND(model={base_home:.3f}, hist={ctx.home_win_pct_home:.3f} → {blended:.3f})"
        )
        home_adj.adjusted_prob = blended
        away_adj.adjusted_prob = 1 - blended
        away_adj.flags.append("AWAY_PROB_RENORMED_AFTER_HOME_SPLIT")

    return home_adj, away_adj


def apply_nba_adjustments(
    ctx: GameContext,
    home_prob: float,
    away_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> tuple[AdjustedProb, AdjustedProb]:
    """Apply all enabled NBA adjustments and return adjusted probs."""
    home_adj, away_adj = nba_rest_adjustment(ctx, home_prob, away_prob, config)
    home_adj, away_adj = nba_home_away_split_adjustment(ctx, home_adj, away_adj, config)
    return home_adj, away_adj


# ---------------------------------------------------------------------------
# Soccer adjustments
# ---------------------------------------------------------------------------

_SOCCER_EURO_FATIGUE_PENALTY = 0.025   # reduce win prob by 2.5% for midweek Euro side
_SOCCER_DRAW_RATE_BASELINE = 0.27      # ~27% of top-flight soccer matches end in draws


def soccer_draw_no_bet(
    home_prob: float,
    draw_prob: float,
    away_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> dict:
    """
    Compute Draw No Bet (DNB) implied probabilities from a 1X2 market.

    DNB eliminates the draw — the stake is returned if the match draws.
    Effective DNB win probability = outcome_prob / (1 - draw_prob).

    Parameters
    ----------
    home_prob, draw_prob, away_prob : float
        No-vig 1X2 true probabilities (must sum to 1.0).

    Returns
    -------
    dict with:
        dnb_home_prob   : float  (prob of home win excl. draw)
        dnb_away_prob   : float
        dnb_home_american : int  (fair DNB odds)
        dnb_away_american : int
        note : str

    Examples
    --------
    >>> r = soccer_draw_no_bet(0.45, 0.27, 0.28)
    >>> round(r["dnb_home_prob"], 4)
    0.6164
    >>> round(r["dnb_away_prob"], 4)
    0.3836
    """
    if not config.get("soccer_draw_no_bet"):
        return {"note": "soccer_draw_no_bet disabled"}

    non_draw = 1 - draw_prob
    dnb_home = home_prob / non_draw
    dnb_away = away_prob / non_draw

    return {
        "dnb_home_prob": round(dnb_home, 4),
        "dnb_away_prob": round(dnb_away, 4),
        "dnb_home_american": decimal_to_american(1 / dnb_home),
        "dnb_away_american": decimal_to_american(1 / dnb_away),
        "note": f"DNB derived from 1X2 (draw={draw_prob:.3f} excluded)",
    }


def soccer_euro_fatigue_adjustment(
    ctx: GameContext,
    home_prob: float,
    away_prob: float,
    draw_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> dict:
    """
    Discount the win probability of teams that played a Champions League
    or Europa League match in the last 4 days (midweek European fixture).

    Penalty is redistributed proportionally between the opponent and draw.

    Parameters
    ----------
    ctx : GameContext
        home_euro_midweek / away_euro_midweek must be populated.

    Returns
    -------
    dict with adjusted home_prob, draw_prob, away_prob, and flags.
    """
    if not config.get("soccer_euro_fatigue"):
        return {
            "home_prob": home_prob, "draw_prob": draw_prob, "away_prob": away_prob,
            "flags": [], "note": "soccer_euro_fatigue disabled",
        }

    flags = []
    h, d, a = home_prob, draw_prob, away_prob

    if ctx.home_euro_midweek:
        penalty = min(_SOCCER_EURO_FATIGUE_PENALTY, h * 0.10)  # cap at 10% of current prob
        h -= penalty
        # Split penalty half to draw, half to away
        d += penalty * 0.5
        a += penalty * 0.5
        flags.append(f"HOME_EURO_FATIGUE(-{penalty:.3f})")

    if ctx.away_euro_midweek:
        penalty = min(_SOCCER_EURO_FATIGUE_PENALTY, a * 0.10)
        a -= penalty
        d += penalty * 0.5
        h += penalty * 0.5
        flags.append(f"AWAY_EURO_FATIGUE(-{penalty:.3f})")

    # Renormalise to sum to 1.0
    total = h + d + a
    h, d, a = h / total, d / total, a / total

    return {
        "home_prob": round(h, 4),
        "draw_prob": round(d, 4),
        "away_prob": round(a, 4),
        "flags": flags,
        "note": "Euro fatigue applied and renormalised",
    }


def apply_soccer_adjustments(
    ctx: GameContext,
    home_prob: float,
    draw_prob: float,
    away_prob: float,
    config: dict = ADJUSTMENT_CONFIG,
) -> dict:
    """
    Apply all enabled soccer adjustments.

    Returns
    -------
    dict with:
        home_prob, draw_prob, away_prob : float  (adjusted)
        dnb                             : dict   (Draw No Bet derived odds)
        flags                           : list
        warnings                        : list
    """
    warnings = []

    if not config.get("soccer_three_way_1x2"):
        warnings.append("soccer_three_way_1x2 disabled — treating as 2-way, ignoring draw")

    # 1. Euro fatigue
    fatigue = soccer_euro_fatigue_adjustment(ctx, home_prob, away_prob, draw_prob, config)
    h = fatigue["home_prob"]
    d = fatigue["draw_prob"]
    a = fatigue["away_prob"]
    flags = fatigue["flags"]

    # 2. Draw No Bet derived odds
    dnb = soccer_draw_no_bet(h, d, a, config)

    return {
        "home_prob": h,
        "draw_prob": d,
        "away_prob": a,
        "dnb": dnb,
        "flags": flags,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

SOCCER_SPORT_KEYS = {
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
}


def apply_adjustments(
    ctx: GameContext,
    probs: list,
    outcome_names: list,
    config: dict = ADJUSTMENT_CONFIG,
) -> dict:
    """
    Unified entry point: route to sport-specific adjustments based on
    ctx.sport_key.

    Parameters
    ----------
    ctx : GameContext
    probs : list of float
        No-vig true probabilities aligned with outcome_names.
    outcome_names : list of str
    config : dict

    Returns
    -------
    dict with:
        adjusted_probs  : list of float
        flags           : list of str
        warnings        : list of str
        extra           : dict  (sport-specific extras like DNB odds)
    """
    sport = ctx.sport_key

    if sport == "icehockey_nhl":
        if len(probs) < 2:
            return {"adjusted_probs": probs, "flags": [], "warnings": ["Not enough probs for NHL"], "extra": {}}
        home_prob, away_prob = probs[0], probs[1]
        home_adj, away_adj = apply_nhl_adjustments(ctx, home_prob, away_prob, config)
        return {
            "adjusted_probs": [home_adj.effective_prob, away_adj.effective_prob],
            "flags": home_adj.flags + away_adj.flags,
            "warnings": home_adj.warnings + away_adj.warnings,
            "confidence_multipliers": [home_adj.confidence_multiplier, away_adj.confidence_multiplier],
            "extra": {},
        }

    elif sport == "basketball_nba":
        if len(probs) < 2:
            return {"adjusted_probs": probs, "flags": [], "warnings": ["Not enough probs for NBA"], "extra": {}}
        home_prob, away_prob = probs[0], probs[1]
        home_adj, away_adj = apply_nba_adjustments(ctx, home_prob, away_prob, config)
        return {
            "adjusted_probs": [home_adj.effective_prob, away_adj.effective_prob],
            "flags": home_adj.flags + away_adj.flags,
            "warnings": home_adj.warnings + away_adj.warnings,
            "confidence_multipliers": [home_adj.confidence_multiplier, away_adj.confidence_multiplier],
            "extra": {},
        }

    elif sport in SOCCER_SPORT_KEYS:
        if len(probs) == 3:
            home_prob, draw_prob, away_prob = probs
        elif len(probs) == 2:
            # h2h without draw (some APIs omit draw)
            home_prob, away_prob = probs
            draw_prob = _SOCCER_DRAW_RATE_BASELINE
            log.warning("Soccer 3-way probs expected, got 2. Using draw baseline %.2f", draw_prob)
        else:
            return {"adjusted_probs": probs, "flags": [], "warnings": ["Unexpected prob count for soccer"], "extra": {}}

        result = apply_soccer_adjustments(ctx, home_prob, draw_prob, away_prob, config)
        adjusted = [result["home_prob"], result["draw_prob"], result["away_prob"]]
        return {
            "adjusted_probs": adjusted,
            "flags": result["flags"],
            "warnings": result["warnings"],
            "confidence_multipliers": [1.0] * len(adjusted),
            "extra": {"dnb": result["dnb"]},
        }

    else:
        log.debug("No sport-specific adjustments for sport_key=%s", sport)
        return {
            "adjusted_probs": probs,
            "flags": [],
            "warnings": [f"No adjustments defined for {sport}"],
            "confidence_multipliers": [1.0] * len(probs),
            "extra": {},
        }


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    print("=" * 60)
    print("NHL — Unconfirmed away goalie")
    print("=" * 60)
    ctx = GameContext(
        sport_key="icehockey_nhl",
        home_team="Boston Bruins",
        away_team="Toronto Maple Leafs",
        away_goalie_confirmed=False,
        home_goalie_confirmed=True,
    )
    result = apply_adjustments(ctx, [0.58, 0.42], ["Boston Bruins", "Toronto Maple Leafs"])
    print(f"  Adjusted probs : {result['adjusted_probs']}")
    print(f"  Flags          : {result['flags']}")
    print(f"  Warnings       : {result['warnings']}")
    print(f"  Conf multipliers: {result['confidence_multipliers']}")

    print()
    print("=" * 60)
    print("NHL — Puck line EV (favorite -1.5)")
    print("=" * 60)
    pl = nhl_puck_line_ev(ml_true_prob=0.65, puck_line_american_odds=-175, is_favorite=True)
    print(f"  {pl['note']}")
    print()
    pl = nhl_puck_line_ev(ml_true_prob=0.35, puck_line_american_odds=155, is_favorite=False)
    print(f"  {pl['note']}")

    print()
    print("=" * 60)
    print("NBA — Away B2B, home rested")
    print("=" * 60)
    ctx = GameContext(
        sport_key="basketball_nba",
        home_team="Denver Nuggets",
        away_team="LA Lakers",
        away_b2b=True,
        home_b2b=False,
        home_win_pct_home=0.68,
    )
    result = apply_adjustments(ctx, [0.52, 0.48], ["Denver Nuggets", "LA Lakers"])
    print(f"  Adjusted probs : {result['adjusted_probs']}")
    print(f"  Flags          : {result['flags']}")

    print()
    print("=" * 60)
    print("Soccer — EPL, away team played Europa on Thursday")
    print("=" * 60)
    ctx = GameContext(
        sport_key="soccer_epl",
        home_team="Arsenal",
        away_team="Manchester United",
        away_euro_midweek=True,
    )
    result = apply_adjustments(ctx, [0.45, 0.27, 0.28], ["Arsenal", "Draw", "Manchester United"])
    print(f"  Adjusted probs : {result['adjusted_probs']}  (sum={sum(result['adjusted_probs']):.4f})")
    print(f"  Flags          : {result['flags']}")
    dnb = result["extra"]["dnb"]
    print(f"  DNB odds       : Arsenal {dnb['dnb_home_american']:+d}  /  Man Utd {dnb['dnb_away_american']:+d}")
    print(f"  DNB probs      : Arsenal {dnb['dnb_home_prob']}  /  Man Utd {dnb['dnb_away_prob']}")

    print()
    print("=" * 60)
    print("Config toggle demo — disable all adjustments")
    print("=" * 60)
    disabled_config = {k: False for k in ADJUSTMENT_CONFIG}
    result = apply_adjustments(ctx, [0.45, 0.27, 0.28], ["Arsenal", "Draw", "Manchester United"], config=disabled_config)
    print(f"  Probs (unchanged): {result['adjusted_probs']}")
    print(f"  Flags            : {result['flags']}")
    print(f"  Warnings         : {result['warnings']}")
