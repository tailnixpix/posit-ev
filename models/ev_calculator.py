"""
ev_calculator.py — Calculate Expected Value for sports betting opportunities.

EV formula (per unit stake):
    EV = (true_prob * profit_if_win) - ((1 - true_prob) * stake)
    EV% = EV / stake * 100

A bet is flagged as +EV when EV% > EV_THRESHOLD (default 3%).

Supports: moneyline (h2h), spreads, totals, player_props
"""

import sys
import os
import logging
from typing import Optional
import pandas as pd

log = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models.no_vig import (
    american_to_decimal,
    american_to_implied,
    no_vig_market,
    sharpest_no_vig,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EV_THRESHOLD_PCT = 2.0   # flag bets with EV% above this value
DEFAULT_STAKE = 100.0     # notional stake used for EV dollar calculation

# Anything above this is almost certainly a data / matching error, not a real edge.
# Professional sharps rarely see sustained EV > 8%; 25% is a hard credibility ceiling.
MAX_CREDIBLE_EV_PCT = 25.0

# Odds cap: suppress heavily-juiced favourites where the risk/reward is poor.
# -250 means you're risking $250 to win $100 — anything worse than this is excluded.
MAX_JUICE_AMERICAN = -250

# ---------------------------------------------------------------------------
# Core EV math
# ---------------------------------------------------------------------------

def expected_value(true_prob: float, american_odds: int, stake: float = DEFAULT_STAKE) -> dict:
    """
    Calculate expected value for a single bet.

    Parameters
    ----------
    true_prob : float
        No-vig (fair) probability of winning in [0, 1].
    american_odds : int
        The odds offered by the bookmaker.
    stake : float
        Notional stake (default $100).

    Returns
    -------
    dict with keys:
        decimal_odds  : float
        profit_if_win : float   (net profit on a win)
        ev            : float   (dollar EV on `stake`)
        ev_pct        : float   (EV as % of stake)
        positive_ev   : bool    (True if ev_pct > EV_THRESHOLD_PCT)

    Examples
    --------
    >>> r = expected_value(0.55, -110)
    >>> round(r["ev_pct"], 2)
    5.95
    >>> r["positive_ev"]
    True

    >>> r = expected_value(0.45, -110)
    >>> round(r["ev_pct"], 2)
    -13.64
    >>> r["positive_ev"]
    False
    """
    # ── Input validation ───────────────────────────────────────────────────────
    if not (0.0 <= true_prob <= 1.0):
        log.error(
            "expected_value: invalid probability %.4f (must be in [0,1]) "
            "for odds=%s — returning zero EV to suppress bad bet.",
            true_prob, american_odds,
        )
        return {
            "decimal_odds": american_to_decimal(american_odds),
            "profit_if_win": 0.0,
            "ev": 0.0,
            "ev_pct": 0.0,
            "positive_ev": False,
        }

    decimal = american_to_decimal(american_odds)
    profit_if_win = stake * (decimal - 1)
    loss_if_lose = stake
    ev = (true_prob * profit_if_win) - ((1 - true_prob) * loss_if_lose)
    ev_pct = (ev / stake) * 100

    # ── Credibility ceiling ────────────────────────────────────────────────────
    # EV above MAX_CREDIBLE_EV_PCT almost always indicates a data/matching error
    # (stale line, wrong game projection attached, prediction-market/sportsbook
    # mismatch).  Cap it so no obviously-wrong bet reaches the dashboard.
    if ev_pct > MAX_CREDIBLE_EV_PCT:
        log.warning(
            "expected_value: EV%.1f%% exceeds credibility ceiling %.1f%% "
            "(prob=%.4f odds=%s) — clamping to 0 to suppress likely bad bet.",
            ev_pct, MAX_CREDIBLE_EV_PCT, true_prob, american_odds,
        )
        return {
            "decimal_odds": decimal,
            "profit_if_win": round(profit_if_win, 2),
            "ev": 0.0,
            "ev_pct": 0.0,
            "positive_ev": False,
        }

    return {
        "decimal_odds": decimal,
        "profit_if_win": round(profit_if_win, 2),
        "ev": round(ev, 2),
        "ev_pct": round(ev_pct, 4),
        "positive_ev": ev_pct > EV_THRESHOLD_PCT,
    }


# ---------------------------------------------------------------------------
# Market-level EV: one book's lines vs. sharp no-vig probs
# ---------------------------------------------------------------------------

def ev_for_market(
    book_american_odds: list,
    true_probs: list,
    outcome_names: list,
    bookmaker: str,
    stake: float = DEFAULT_STAKE,
) -> list:
    """
    Compute EV for every outcome in a single market.

    Parameters
    ----------
    book_american_odds : list of int
        The odds offered by this bookmaker for each outcome.
    true_probs : list of float
        No-vig true probabilities (must align with book_american_odds).
    outcome_names : list of str
    bookmaker : str
    stake : float

    Returns
    -------
    list of dicts — one per outcome.
    """
    results = []
    for odds, prob, name in zip(book_american_odds, true_probs, outcome_names):
        ev = expected_value(prob, odds, stake)
        results.append({
            "bookmaker": bookmaker,
            "outcome_name": name,
            "american_odds": odds,
            "true_prob": round(prob, 4),
            "implied_prob": round(american_to_implied(odds), 4),
            **ev,
        })
    return results


# ---------------------------------------------------------------------------
# Full pipeline: odds DataFrame → +EV DataFrame
# ---------------------------------------------------------------------------

def find_positive_ev(
    odds_df: pd.DataFrame,
    market: str = "h2h",
    ev_threshold: float = EV_THRESHOLD_PCT,
    stake: float = DEFAULT_STAKE,
) -> pd.DataFrame:
    """
    Scan all games in odds_df for a given market, compute no-vig true
    probabilities using the sharpest book, then evaluate every book's
    lines for +EV opportunities.

    Parameters
    ----------
    odds_df : pd.DataFrame
        Output of odds_fetcher.get_odds_df().
    market : str
        "h2h", "spreads", "totals", or "player_props".
    ev_threshold : float
        Minimum EV% to flag as a positive EV bet (default 3.0).
    stake : float
        Notional stake for EV dollar calculation.

    Returns
    -------
    pd.DataFrame
        All bets with ev_pct > ev_threshold, sorted descending by ev_pct.
        Columns: game, market, bookmaker, outcome_name, american_odds,
                 true_prob, implied_prob, ev, ev_pct, positive_ev,
                 commence_time, sport_key.
    """
    subset = odds_df[odds_df["market"] == market].copy()
    if subset.empty:
        return pd.DataFrame()

    all_rows = []

    for game_id, game_df in subset.groupby("game_id"):
        meta = game_df.iloc[0]
        game_label = f"{meta['away_team']} @ {meta['home_team']}"

        # Build {bookmaker: [odds per outcome]} — outcomes sorted for alignment
        outcome_order = sorted(game_df["outcome_name"].unique())
        book_odds = {}        # all books — used for EV evaluation
        sportsbook_odds = {}  # sportsbooks only — used as sharp reference
        for book, bk_df in game_df.groupby("bookmaker"):
            bk_df_sorted = bk_df.set_index("outcome_name").reindex(outcome_order)
            if bk_df_sorted["price"].isna().any():
                continue  # skip books missing an outcome
            odds_list = bk_df_sorted["price"].astype(int).tolist()
            book_odds[book] = odds_list
            # Prediction markets (NoVig, Kalshi, Polymarket) must not anchor the
            # true-probability reference for spread/total markets — their pricing
            # models differ structurally from sportsbooks on non-h2h markets and
            # their near-zero vig always makes them "sharpest" even when wrong.
            # For h2h they compete directly and are legitimately included.
            src = bk_df["source_type"].iloc[0] if "source_type" in bk_df.columns else "sportsbook"
            if src == "sportsbook" or market == "h2h":
                sportsbook_odds[book] = odds_list

        if len(book_odds) < 2:
            continue  # need at least two books to identify the sharpest

        # Use sportsbooks as the sharp reference for spreads/totals.
        # Fall back to all books only when fewer than 2 sportsbooks have data.
        reference_odds = sportsbook_odds if len(sportsbook_odds) >= 2 else book_odds
        if len(reference_odds) < 2:
            continue

        # --- True probabilities from sharpest sportsbook ---
        sharp = sharpest_no_vig(reference_odds, outcome_names=outcome_order)
        true_probs = sharp["no_vig_probs"]
        sharp_book = sharp["sharpest_book"]

        # --- Evaluate every book's lines against those true probs ---
        for book, odds_list in book_odds.items():
            rows = ev_for_market(odds_list, true_probs, outcome_order, book, stake)
            for row in rows:
                # Carry the spread/total line value through from the raw odds data
                outcome_match = game_df[
                    (game_df["bookmaker"] == book) &
                    (game_df["outcome_name"] == row["outcome_name"])
                ]
                point_val = None
                if not outcome_match.empty and "point" in outcome_match.columns:
                    raw_point = outcome_match["point"].iloc[0]
                    if raw_point is not None and str(raw_point) not in ("nan", "None", ""):
                        try:
                            point_val = float(raw_point)
                        except (ValueError, TypeError):
                            pass

                # Resolve source_type from the original data for this bookmaker
                source_type_val = "sportsbook"
                if not outcome_match.empty and "source_type" in outcome_match.columns:
                    source_type_val = outcome_match["source_type"].iloc[0] or "sportsbook"
                elif "source_type" in game_df.columns:
                    bk_rows = game_df[game_df["bookmaker"] == book]
                    if not bk_rows.empty:
                        source_type_val = bk_rows["source_type"].iloc[0] or "sportsbook"

                row.update({
                    "game_id": game_id,
                    "game": game_label,
                    "market": market,
                    "sport_key": meta["sport_key"],
                    "commence_time": meta["commence_time"],
                    "sharp_book": sharp_book,
                    "sharp_vig_pct": round(sharp["sharpest_vig"] * 100, 3),
                    "point": point_val,
                    "source_type": source_type_val,
                })
                all_rows.append(row)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    positive = df[
        (df["ev_pct"] > ev_threshold) &
        (df["american_odds"] >= MAX_JUICE_AMERICAN)
    ].copy()
    return positive.sort_values("ev_pct", ascending=False).reset_index(drop=True)


def find_all_positive_ev(
    odds_df: pd.DataFrame,
    markets: list = None,
    ev_threshold: float = EV_THRESHOLD_PCT,
    stake: float = DEFAULT_STAKE,
) -> pd.DataFrame:
    """
    Run find_positive_ev across all markets and concatenate results.

    Parameters
    ----------
    odds_df : pd.DataFrame
    markets : list of str, optional
        Defaults to ["h2h", "spreads", "totals"].
    ev_threshold : float
    stake : float

    Returns
    -------
    pd.DataFrame — all +EV bets across all markets, sorted by ev_pct.
    """
    markets = markets or ["h2h", "spreads", "totals"]
    frames = []
    for mkt in markets:
        result = find_positive_ev(odds_df, market=mkt, ev_threshold=ev_threshold, stake=stake)
        if not result.empty:
            frames.append(result)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    return combined.sort_values("ev_pct", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Player props EV pipeline
# ---------------------------------------------------------------------------

def find_positive_ev_props(
    props_df: pd.DataFrame,
    ev_threshold: float = EV_THRESHOLD_PCT,
    stake: float = DEFAULT_STAKE,
) -> pd.DataFrame:
    """
    Scan player props DataFrame for +EV opportunities.

    Groups by (game_id, prop_market, player, point) — each unique player line
    is its own mini-market with Over and Under as the two outcomes.

    Sharp reference strategy (mirrors the game-market approach):
    - Exchanges (Novig, ProphetX) and sportsbooks together form the reference pool
      for sharpest_no_vig so exchange pricing anchors the true probability.
    - Only sportsbook bets are emitted in the output — exchanges are not shown as
      actionable bets since not all users have accounts there.
    - Requires at least 2 books in the reference pool.
    """
    if props_df.empty:
        return pd.DataFrame()

    # Books whose source_type is "sportsbook" — these are evaluated for EV and
    # shown to users as actionable bets.
    SPORTSBOOK_SOURCE = "sportsbook"

    all_rows = []
    # Skip counters — emitted as a single INFO log at the end for Railway visibility
    _skip_outcomes = 0   # group had != 2 outcomes
    _skip_books    = 0   # < 2 books or no sportsbooks
    _total_groups  = 0

    for keys, group in props_df.groupby(
        ["game_id", "prop_market", "player", "point"], dropna=False
    ):
        _total_groups += 1
        game_id, prop_market, player, point = keys
        meta = group.iloc[0]
        game_label = f"{meta['away_team']} @ {meta['home_team']}"
        outcome_order = sorted(group["outcome_name"].unique())

        if len(outcome_order) != 2:
            _skip_outcomes += 1
            log.debug(
                "Props EV skip [outcomes≠2]: %s | %s | %s | outcomes=%s",
                game_label, prop_market, player, list(outcome_order),
            )
            continue  # props must have Over and Under

        book_odds: dict = {}        # all books — used for the sharp reference
        sportsbook_odds: dict = {}  # sportsbooks only — used for EV evaluation / output

        for book, bk_df in group.groupby("bookmaker"):
            # Some books return duplicate outcome rows for the same line —
            # keep the first occurrence to avoid set_index duplicate-label crash.
            bk_df = bk_df.drop_duplicates(subset=["outcome_name"])
            bk_df_sorted = bk_df.set_index("outcome_name").reindex(outcome_order)
            if bk_df_sorted["price"].isna().any():
                continue
            odds_list = bk_df_sorted["price"].astype(int).tolist()
            book_odds[book] = odds_list

            src = (
                bk_df["source_type"].iloc[0]
                if "source_type" in bk_df.columns
                else SPORTSBOOK_SOURCE
            )
            if src == SPORTSBOOK_SOURCE:
                sportsbook_odds[book] = odds_list

        # Need at least 2 books total (e.g. Novig + one sportsbook) for a
        # meaningful sharp reference, and at least 1 sportsbook to emit a bet.
        if len(book_odds) < 2 or not sportsbook_odds:
            _skip_books += 1
            log.debug(
                "Props EV skip [books]: %s | %s | %s | book_odds=%d sportsbook_odds=%d",
                game_label, prop_market, player, len(book_odds), len(sportsbook_odds),
            )
            continue

        sharp = sharpest_no_vig(book_odds, outcome_names=outcome_order)
        true_probs = sharp["no_vig_probs"]
        sharp_book = sharp["sharpest_book"]

        # Evaluate EV for sportsbooks only — exchanges anchor the reference but
        # are not surfaced as bets.
        for book, odds_list in sportsbook_odds.items():
            rows = ev_for_market(odds_list, true_probs, outcome_order, book, stake)
            for row in rows:
                point_val = None
                if point is not None:
                    try:
                        pf = float(point)
                        if str(pf) not in ("nan", "inf"):
                            point_val = pf
                    except (ValueError, TypeError):
                        pass

                row.update({
                    "game_id":          game_id,
                    "game":             game_label,
                    "market":           prop_market,
                    "player_name":      player,
                    "sport_key":        meta["sport_key"],
                    "commence_time":    meta["commence_time"],
                    "sharp_book":       sharp_book,
                    "sharp_vig_pct":    round(sharp["sharpest_vig"] * 100, 3),
                    "point":            point_val,
                    "source_type":      SPORTSBOOK_SOURCE,
                    "is_prop":          True,
                    "adjusted_prob":    None,
                    "confidence_mult":  1.0,
                    "adj_flags":        "",
                    "adj_warnings":     "",
                    "effective_ev_pct": None,
                })
                all_rows.append(row)

    log.info(
        "Props EV: %d groups total — %d skipped (outcomes≠2), %d skipped (<2 books), %d had sportsbook EV rows",
        _total_groups, _skip_outcomes, _skip_books, len(all_rows),
    )

    if not all_rows:
        log.warning(
            "Props EV: no sportsbook rows generated. "
            "Likely causes: all player lines covered by <2 books, or all lines have wrong outcome count."
        )
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["effective_ev_pct"] = df["ev_pct"]
    df["adjusted_prob"] = df["true_prob"]
    positive = df[
        (df["ev_pct"] > ev_threshold) &
        (df["american_odds"] >= MAX_JUICE_AMERICAN)
    ].copy()
    log.info(
        "Props EV: %d total sportsbook rows → %d meet +EV threshold (>%.1f%% EV, odds≥%d)",
        len(df), len(positive), ev_threshold, MAX_JUICE_AMERICAN,
    )
    return positive.sort_values("ev_pct", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

DISPLAY_COLS = [
    "game", "market", "outcome_name", "bookmaker",
    "american_odds", "true_prob", "implied_prob",
    "ev", "ev_pct", "commence_time",
]


def print_ev_report(ev_df: pd.DataFrame, title: str = "+EV Opportunities") -> None:
    """Pretty-print the +EV DataFrame to stdout."""
    if ev_df.empty:
        print(f"\n[{title}] No +EV bets found above threshold.")
        return

    cols = [c for c in DISPLAY_COLS if c in ev_df.columns]
    print(f"\n{'=' * 70}")
    print(f" {title}  ({len(ev_df)} bets)")
    print(f"{'=' * 70}")
    print(ev_df[cols].to_string(index=False))
    print(f"\nAvg EV%: {ev_df['ev_pct'].mean():.2f}%   "
          f"Max EV%: {ev_df['ev_pct'].max():.2f}%   "
          f"Total EV on ${int(DEFAULT_STAKE)}/bet: ${ev_df['ev'].sum():.2f}")


# ---------------------------------------------------------------------------
# CLI / example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    # --- Standalone unit tests ---
    print("=" * 60)
    print("UNIT TESTS")
    print("=" * 60)

    # Test 1: genuine +EV bet
    r = expected_value(0.55, -110)
    assert r["positive_ev"], "Should be +EV"
    print(f"[PASS] true_prob=0.55 @ -110 → EV%={r['ev_pct']}%  positive={r['positive_ev']}")

    # Test 2: negative EV
    r = expected_value(0.45, -110)
    assert not r["positive_ev"], "Should be -EV"
    print(f"[PASS] true_prob=0.45 @ -110 → EV%={r['ev_pct']}%  positive={r['positive_ev']}")

    # Test 3: 3-way soccer market with realistic overround > 1
    # Odds: home -130, draw +280, away +200 → overround ~1.047
    soccer_market = no_vig_market([-130, 280, 200], ["Home", "Draw", "Away"])
    true_probs = soccer_market["no_vig_probs"]
    og = soccer_market["overround"]
    # When overround > 1, evaluating a book's own odds against its own no-vig probs
    # yields slightly negative EV (true_prob < implied_prob for each outcome).
    # Expect small magnitude EVs, not exactly 0.
    rows = ev_for_market([-130, 280, 200], true_probs, ["Home", "Draw", "Away"], "testbook")
    assert og > 1.0, f"Expected overround > 1, got {og}"
    for row in rows:
        assert row["ev_pct"] < 0, f"Self-eval on viggy book should be -EV, got {row['ev_pct']}"
    print(f"[PASS] Soccer 1X2 (overround={round(og,4)}) self-eval EVs (expect negative): {[r['ev_pct'] for r in rows]}")

    # Test 4: live data from API
    print()
    print("=" * 60)
    print("LIVE TEST: NBA +EV scan")
    print("=" * 60)

    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default="basketball_nba")
    parser.add_argument("--threshold", type=float, default=EV_THRESHOLD_PCT)
    args = parser.parse_args()

    from scripts.odds_fetcher import get_odds_df
    df = get_odds_df(sport_keys=[args.sport], markets=["h2h", "spreads", "totals"])

    if df.empty:
        print("No live data available.")
    else:
        ev_df = find_all_positive_ev(df, ev_threshold=args.threshold)
        print_ev_report(ev_df, title=f"+EV Bets — {args.sport} (threshold={args.threshold}%)")
