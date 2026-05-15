"""
sharp_signals.py — Compute sharp-money conviction signals for each +EV bet.

Signals
-------
A. rlm            — Reverse Line Movement: public fades our bet but line moved our way
B. steam_bps      — Basis-point magnitude of line movement toward this bet since opening
E. line_shop_bps  — How much better our odds are vs the market average (basis points)
F. clv_grade      — Closing Line Value grade (A–F) proxied from EV%
G. sharp_grade    — Composite conviction grade (S/A/B/C/D) blending all signals
K. pred_mkt_note  — Prediction-market alignment (Kalshi / Polymarket vs model)
"""

import json
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Prediction-market bookmaker keys as stored in all_book_odds JSON
_PRED_MKT_BOOKS = {"kalshi", "polymarket"}


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _american_to_implied(odds: int) -> float:
    """Convert American odds integer to vig-on implied probability."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


# ---------------------------------------------------------------------------
# Signal A — Reverse Line Movement
# ---------------------------------------------------------------------------

def compute_rlm(
    bet_pct: Optional[float],
    opening_odds: Optional[int],
    current_odds: Optional[int],
) -> tuple[bool, Optional[str]]:
    """
    Detect Reverse Line Movement.

    RLM fires when ≥60% of the public is fading our bet (bet_pct < 0.40)
    AND the line has moved ≥1.5 percentage-points in our favour since opening.
    That combination means sharp money overrode the public.

    Returns (is_rlm, note_string).
    """
    if bet_pct is None or opening_odds is None or current_odds is None:
        return False, None
    if opening_odds == current_odds:
        return False, None

    opening_imp = _american_to_implied(opening_odds)
    current_imp = _american_to_implied(current_odds)

    line_moved_our_way = current_imp < opening_imp - 0.015   # ≥1.5 pp improvement
    public_fading      = bet_pct < 0.40                       # ≥60 % against us

    if public_fading and line_moved_our_way:
        public_pct = int((1.0 - bet_pct) * 100)
        op_str = f"{'+' if opening_odds > 0 else ''}{opening_odds}"
        cu_str = f"{'+' if current_odds > 0 else ''}{current_odds}"
        return True, f"{public_pct}% public against · line moved {op_str}→{cu_str}"

    return False, None


# ---------------------------------------------------------------------------
# Signal B — Steam (line movement magnitude)
# ---------------------------------------------------------------------------

def compute_steam(
    opening_odds: Optional[int],
    current_odds: Optional[int],
) -> tuple[int, Optional[str]]:
    """
    Measure how many basis points the line has moved toward this bet since open.

    Positive bps = line got cheaper / better for the bettor (sharp action our way).
    Returns (bps_moved, note_string).  note_string is None when move < 150 bps.
    """
    if opening_odds is None or current_odds is None or opening_odds == current_odds:
        return 0, None

    opening_imp = _american_to_implied(opening_odds)
    current_imp = _american_to_implied(current_odds)
    bps = int((opening_imp - current_imp) * 10_000)

    if bps >= 150:
        op_str = f"{'+' if opening_odds > 0 else ''}{opening_odds}"
        cu_str = f"{'+' if current_odds > 0 else ''}{current_odds}"
        return bps, f"Line steamed {op_str}→{cu_str} (+{bps}bps)"

    return bps, None


# ---------------------------------------------------------------------------
# Signal E — Line Shopping Score
# ---------------------------------------------------------------------------

def compute_line_shop_bps(
    all_book_odds_json: Optional[str],
    current_odds: int,
    current_book: str,
) -> int:
    """
    Compute how many basis points better our odds are vs the sportsbook average.

    Parses all_book_odds JSON ({"draftkings": -110, "fanduel": -115, ...}),
    converts each book's odds to implied probability, averages them (excluding
    prediction markets), then returns (avg_implied − our_implied) × 10 000.

    Positive = our book is paying less vig / offers a better price than average.
    """
    if not all_book_odds_json:
        return 0

    try:
        book_map: dict = json.loads(all_book_odds_json)
    except (json.JSONDecodeError, TypeError):
        return 0

    sportsbook_odds = [
        v for k, v in book_map.items()
        if k.lower() not in _PRED_MKT_BOOKS and isinstance(v, (int, float))
    ]

    if len(sportsbook_odds) < 2:
        return 0

    avg_implied = sum(_american_to_implied(int(o)) for o in sportsbook_odds) / len(sportsbook_odds)
    our_implied = _american_to_implied(current_odds)
    return int((avg_implied - our_implied) * 10_000)


# ---------------------------------------------------------------------------
# Signal F — CLV Grade
# ---------------------------------------------------------------------------

def compute_clv_grade(ev_pct: float) -> str:
    """
    Grade Closing Line Value potential (A–F) using EV% as the proxy.

    EV% measures how far our price sits from the no-vig sharp consensus —
    the same distance that predicts beating the closing line.
    """
    if ev_pct >= 8:
        return "A"
    if ev_pct >= 5:
        return "B"
    if ev_pct >= 3:
        return "C"
    if ev_pct >= 2:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Signal K — Prediction Market Consensus
# ---------------------------------------------------------------------------

def compute_pred_mkt(
    all_book_odds_json: Optional[str],
    true_prob: float,
) -> tuple[bool, Optional[str]]:
    """
    Check whether prediction markets (Kalshi / Polymarket) agree with our model.

    Returns (aligned: bool, note: str | None).
    aligned = True when their implied prob is within 5 pp of true_prob.
    """
    if not all_book_odds_json:
        return False, None

    try:
        book_map: dict = json.loads(all_book_odds_json)
    except (json.JSONDecodeError, TypeError):
        return False, None

    pm_entries = {
        k: v for k, v in book_map.items()
        if k.lower() in _PRED_MKT_BOOKS and isinstance(v, (int, float))
    }

    if not pm_entries:
        return False, None

    pm_probs = {k: _american_to_implied(int(v)) for k, v in pm_entries.items()}
    avg_pm_prob = sum(pm_probs.values()) / len(pm_probs)
    divergence  = abs(avg_pm_prob - true_prob)

    parts = [f"{k.capitalize()}: {p * 100:.0f}%" for k, p in pm_probs.items()]
    note  = " · ".join(parts)

    return divergence <= 0.05, note


# ---------------------------------------------------------------------------
# Signal G — Composite Sharp Grade
# ---------------------------------------------------------------------------

def compute_sharp_grade(
    ev_pct: float,
    rlm: bool,
    steam_bps: int,
    line_shop_bps: int,
    pred_mkt_aligned: bool,
    clv_grade: str,
) -> str:
    """
    Composite conviction grade (S / A / B / C / D) blending all signals.

    Weights
    -------
    EV%          → 0–40 pts  (capped at 10 % EV = 40 pts)
    RLM          → 25 pts    (sharp money confirmed vs public)
    Steam        → 0–15 pts  (line moved ≥150 bps, capped at 300 bps)
    Line shop    → 0–10 pts  (best price vs market avg, capped at 200 bps)
    Pred market  → 5 pts     (Kalshi/Polymarket agrees within 5 pp)
    CLV grade    → 0–5 pts   (A=5, B=3, C=2, D=1, F=0)

    Thresholds:  S ≥ 70 · A ≥ 52 · B ≥ 37 · C ≥ 22 · D < 22
    """
    score = min(40.0, ev_pct * 4.0)

    if rlm:
        score += 25.0

    if steam_bps > 0:
        score += min(15.0, steam_bps / 20.0)

    if line_shop_bps > 0:
        score += min(10.0, line_shop_bps / 20.0)

    if pred_mkt_aligned:
        score += 5.0

    score += {"A": 5, "B": 3, "C": 2, "D": 1, "F": 0}.get(clv_grade, 0)

    if score >= 70:
        return "S"
    if score >= 52:
        return "A"
    if score >= 37:
        return "B"
    if score >= 22:
        return "C"
    return "D"
