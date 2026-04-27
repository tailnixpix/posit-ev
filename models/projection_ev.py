"""
projection_ev.py — EV calculation using Optimal model projections as the true
probability source.

Rather than deriving "true probability" from the no-vig sportsbook consensus, this
module uses Optimal's game projections (home_win_probability, total_mean, spread_mean
plus percentile bands) as the ground-truth probability distribution.

This eliminates the mismatch where a projected score (e.g. 3.6–2.6 = 6.2 total) and
the displayed EV bet direction (e.g. Under 6.0) contradict each other.  The model
is always internally consistent: if it projects 6.2 it can only ever surface Over
bets for that game.

For totals and spreads we convert the model's mean + percentile distribution into
a win probability via a normal CDF.  Sport-specific default sigmas are used as
fallback when Optimal's p25/p75 percentiles are not available.
"""

import logging
import math
from typing import Optional

import pandas as pd

from models.ev_calculator import (
    expected_value,
    EV_THRESHOLD_PCT,
    DEFAULT_STAKE,
    MAX_JUICE_AMERICAN,
)
from models.no_vig import american_to_implied

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sport-specific default standard deviations
# Used when Optimal p25/p75 percentiles are unavailable.
# ---------------------------------------------------------------------------
_DEFAULT_SIGMA: dict = {
    "total": {
        "icehockey_nhl":              1.50,
        "basketball_nba":            11.00,
        "baseball_mlb":               2.00,
        "soccer_epl":                 1.40,
        "soccer_spain_la_liga":       1.40,
        "soccer_germany_bundesliga":  1.40,
        "soccer_usa_mls":             1.40,
    },
    "spread": {
        "icehockey_nhl":              1.40,
        "basketball_nba":            10.00,
        "baseball_mlb":               2.00,
        "soccer_epl":                 1.30,
        "soccer_spain_la_liga":       1.30,
        "soccer_germany_bundesliga":  1.30,
        "soccer_usa_mls":             1.30,
    },
}


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF (no scipy required)."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


_MIN_SIGMA = 0.30   # floor — prevents extreme z-scores from near-zero spread distributions

def _sigma_from_percentiles(p25: Optional[float], p75: Optional[float]) -> Optional[float]:
    """
    Estimate σ from IQR.
    For a normal distribution:  IQR = Q75 − Q25 = 1.3490 × σ
    Returns None when percentiles are missing or produce a degenerate range.
    """
    if p25 is None or p75 is None:
        return None
    iqr = float(p75) - float(p25)
    if iqr <= 0:
        return None
    sigma = iqr / 1.3490
    return max(sigma, _MIN_SIGMA)   # never below the floor


def _get_sigma(sport_key: str, market_type: str,
               p25: Optional[float], p75: Optional[float]) -> float:
    """
    Return the best available σ for the given sport / market.
    Prefers the IQR-derived σ from Optimal percentiles; falls back to the
    sport-specific defaults above.
    """
    from_pct = _sigma_from_percentiles(p25, p75)
    if from_pct is not None:
        return from_pct
    defaults = _DEFAULT_SIGMA.get(market_type, {})
    fallback_key = next(iter(defaults), "icehockey_nhl")
    sigma = defaults.get(sport_key, _DEFAULT_SIGMA[market_type].get(fallback_key, 1.5))
    return max(sigma, _MIN_SIGMA)   # enforce floor on defaults too


def _word_set(s: str) -> set:
    """Lower-case word tokens, stripping common stop-words."""
    skip = {"at", "the", "a", "an", "vs", "fc", "sc", "city", "united", "de"}
    return {w for w in s.lower().split() if w not in skip and len(w) > 2}


# ---------------------------------------------------------------------------
# Core converter: projection dict → true probability
# ---------------------------------------------------------------------------

def projection_to_true_prob(
    proj: dict,
    market: str,
    outcome_name: str,
    point: Optional[float],
    sport_key: str,
) -> Optional[float]:
    """
    Convert an Optimal game projection dict into a true win probability for a
    specific market outcome.

    Parameters
    ----------
    proj : dict
        Output of fetch_game_projections() — keys include:
        home_win_probability, total_mean, total_p25, total_p75,
        spread_mean, spread_p25, spread_p75,
        home_score_mean, away_score_mean, home_team, away_team.
    market : str
        "h2h", "totals", or "spreads".
    outcome_name : str
        For h2h / spreads: team name (e.g. "Boston Bruins").
        For totals: "Over …" or "Under …".
    point : float | None
        The spread or total line value from the bookmaker.
    sport_key : str
        e.g. "icehockey_nhl".

    Returns
    -------
    float in [0, 1] or None if the projection is insufficient for this market.
    """
    if not proj:
        return None

    home_team = (proj.get("home_team") or "").lower()
    away_team = (proj.get("away_team") or "").lower()
    out_lower = outcome_name.lower()

    # ── Moneyline (h2h) ──────────────────────────────────────────────────────
    if market == "h2h":
        hwp = proj.get("home_win_probability")
        if hwp is None:
            return None

        hwp = float(hwp)
        # Guard: Optimal stores this as 0-1; if it ever arrives as 0-100
        # (percentage scale), detect and normalise rather than blow up EV.
        if hwp > 1.0:
            if hwp <= 100.0:
                log.warning(
                    "projection_to_true_prob: home_win_probability=%.4f looks like "
                    "a percentage — dividing by 100.", hwp
                )
                hwp /= 100.0
            else:
                log.error(
                    "projection_to_true_prob: home_win_probability=%.4f is out of "
                    "range [0, 100] — skipping.", hwp
                )
                return None
        if hwp < 0.0:
            log.error("projection_to_true_prob: negative home_win_probability %.4f — skipping.", hwp)
            return None

        home_words = _word_set(home_team)
        away_words = _word_set(away_team)
        out_words  = _word_set(out_lower)

        home_score = len(home_words & out_words)
        away_score = len(away_words & out_words)

        if home_score > away_score:
            return float(hwp)
        elif away_score > home_score:
            return float(1.0 - hwp)
        else:
            # Ambiguous — can't determine home vs away safely
            return None

    # ── Totals ───────────────────────────────────────────────────────────────
    if market == "totals":
        total_mean = proj.get("total_mean")
        if total_mean is None or point is None:
            return None

        sigma = _get_sigma(
            sport_key, "total",
            proj.get("total_p25"), proj.get("total_p75"),
        )
        z = (float(point) - float(total_mean)) / sigma
        over_prob = 1.0 - _norm_cdf(z)

        if out_lower.startswith("over"):
            return float(over_prob)
        else:
            return float(1.0 - over_prob)

    # ── Spreads ──────────────────────────────────────────────────────────────
    if market == "spreads":
        if point is None:
            return None

        # Projected home margin (positive = home wins)
        hs  = proj.get("home_score_mean")
        as_ = proj.get("away_score_mean")
        if hs is not None and as_ is not None:
            proj_margin = float(hs) - float(as_)
        elif proj.get("spread_mean") is not None:
            proj_margin = float(proj["spread_mean"])
        else:
            return None

        sigma = _get_sigma(
            sport_key, "spread",
            proj.get("spread_p25"), proj.get("spread_p75"),
        )

        home_words = _word_set(home_team)
        away_words = _word_set(away_team)
        out_words  = _word_set(out_lower)

        home_score = len(home_words & out_words)
        away_score = len(away_words & out_words)

        if home_score <= 0 and away_score <= 0:
            # Can't determine side — fall back
            return None

        is_home = home_score > away_score

        if is_home:
            # Home covers when home_margin > −point
            # (point = −3.5 for home favorite, so −point = 3.5)
            z = (-float(point) - proj_margin) / sigma
            return float(1.0 - _norm_cdf(z))
        else:
            # Away covers when home_margin < point
            # (point = +3.5 for away dog, so away covers if home wins by < 3.5)
            z = (float(point) - proj_margin) / sigma
            return float(_norm_cdf(z))

    return None


# ---------------------------------------------------------------------------
# Model-based EV finder
# ---------------------------------------------------------------------------

def find_positive_ev_model(
    odds_df: pd.DataFrame,
    proj_map: dict,
    markets: list = None,
    ev_threshold: float = EV_THRESHOLD_PCT,
    stake: float = DEFAULT_STAKE,
) -> pd.DataFrame:
    """
    Find +EV bets using Optimal model projections as the true probability.

    For each game in odds_df that has a projection in proj_map, the model
    probability is computed per book (accounting for each book's specific
    spread/total line) and EV is evaluated against that book's odds.

    Games without projection data (e.g. soccer, games not yet in Optimal)
    fall back to the traditional no-vig consensus approach.

    Parameters
    ----------
    odds_df : pd.DataFrame
        Output of odds_fetcher.get_odds_df().
    proj_map : dict
        {game_id: projection_dict} — from _build_projection_map().
    markets : list of str
        Defaults to ["h2h", "spreads", "totals"].
    ev_threshold : float
        Minimum EV% to surface a bet.
    stake : float
        Notional stake for EV dollar calculation.

    Returns
    -------
    pd.DataFrame — all +EV bets, sorted descending by ev_pct.
    Columns include prob_source ("model" or "no_vig") so callers can distinguish.
    """
    from models.ev_calculator import find_positive_ev as _no_vig_ev

    markets = markets or ["h2h", "spreads", "totals"]

    all_frames: list = []

    for mkt in markets:
        subset = odds_df[odds_df["market"] == mkt].copy()
        if subset.empty:
            continue

        model_rows: list = []
        fallback_game_ids: list = []

        for game_id, game_df in subset.groupby("game_id"):
            proj = proj_map.get(str(game_id))
            if not proj:
                fallback_game_ids.append(game_id)
                continue

            meta = game_df.iloc[0]
            game_label = f"{meta['away_team']} @ {meta['home_team']}"
            sport_key  = str(meta["sport_key"])

            # For 3-way soccer markets (Home / Draw / Away) Optimal only gives
            # home_win_probability without a draw split — fall back to no-vig.
            unique_outcomes = game_df["outcome_name"].nunique()
            if unique_outcomes > 2 and mkt == "h2h":
                fallback_game_ids.append(game_id)
                continue

            had_model_prob = False

            for book, bk_df in game_df.groupby("bookmaker"):
                src = "sportsbook"
                if "source_type" in bk_df.columns and not bk_df["source_type"].isna().all():
                    src = bk_df["source_type"].iloc[0] or "sportsbook"

                for _, out_row in bk_df.iterrows():
                    outcome = str(out_row["outcome_name"])
                    try:
                        odds = int(out_row["price"])
                    except (ValueError, TypeError):
                        continue

                    point_val = None
                    if "point" in out_row and out_row["point"] is not None:
                        try:
                            raw = out_row["point"]
                            if str(raw) not in ("nan", "None", ""):
                                point_val = float(raw)
                        except (ValueError, TypeError):
                            pass

                    prob = projection_to_true_prob(proj, mkt, outcome, point_val, sport_key)
                    if prob is None:
                        continue

                    had_model_prob = True
                    ev = expected_value(prob, odds, stake)

                    if ev["ev_pct"] > ev_threshold and odds >= MAX_JUICE_AMERICAN:
                        model_rows.append({
                            "game_id":       str(game_id),
                            "game":          game_label,
                            "market":        mkt,
                            "sport_key":     sport_key,
                            "commence_time": meta["commence_time"],
                            "bookmaker":     book,
                            "outcome_name":  outcome,
                            "american_odds": odds,
                            "true_prob":     round(prob, 4),
                            "implied_prob":  round(american_to_implied(odds), 4),
                            "decimal_odds":  ev["decimal_odds"],
                            "profit_if_win": ev["profit_if_win"],
                            "ev":            ev["ev"],
                            "ev_pct":        round(ev["ev_pct"], 4),
                            "positive_ev":   True,
                            "point":         point_val,
                            "source_type":   src,
                            "sharp_book":    "model",
                            "sharp_vig_pct": 0.0,
                            "prob_source":   "model",
                        })

            if not had_model_prob:
                # No probability could be derived from projections → no-vig fallback
                fallback_game_ids.append(game_id)

        # ── No-vig fallback for un-projected games ────────────────────────────
        if fallback_game_ids:
            fb_subset = subset[subset["game_id"].isin(fallback_game_ids)]
            if not fb_subset.empty:
                fb_df = _no_vig_ev(
                    fb_subset, market=mkt,
                    ev_threshold=ev_threshold, stake=stake,
                )
                if not fb_df.empty:
                    fb_df["prob_source"] = "no_vig"
                    all_frames.append(fb_df)

        if model_rows:
            model_df = pd.DataFrame(model_rows)
            all_frames.append(model_df)

    if not all_frames:
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)
    return combined.sort_values("ev_pct", ascending=False).reset_index(drop=True)
