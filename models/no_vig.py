"""
no_vig.py — Convert American odds to true (no-vig) probabilities.

Supports:
  - 2-way markets: moneyline (NBA/NHL), spreads, totals
  - 3-way markets: soccer 1X2 (home / draw / away)

Vig removal method: multiplicative (divide each implied prob by the overround).
This preserves the relative shape of the market and is preferred over
the additive method for asymmetric markets (e.g. heavy favorites).

Key functions
-------------
american_to_implied(odds)         -> float
overround(implied_probs)          -> float
remove_vig(implied_probs)         -> list[float]
no_vig_market(american_odds_list) -> dict
sharpest_no_vig(book_odds_dict)   -> dict
"""

from typing import Union


# ---------------------------------------------------------------------------
# Core conversions
# ---------------------------------------------------------------------------

def american_to_decimal(odds: int) -> float:
    """
    Convert American odds to decimal odds.

    Parameters
    ----------
    odds : int
        American odds (e.g. -110, +150).

    Returns
    -------
    float
        Decimal odds (e.g. 1.909, 2.5).

    Examples
    --------
    >>> american_to_decimal(-110)
    1.9090909090909092
    >>> american_to_decimal(150)
    2.5
    """
    if odds > 0:
        return (odds / 100) + 1
    return (100 / abs(odds)) + 1


def american_to_implied(odds: int) -> float:
    """
    Convert American odds to raw implied probability (includes vig).

    Parameters
    ----------
    odds : int
        American odds.

    Returns
    -------
    float
        Implied probability in [0, 1].

    Examples
    --------
    >>> round(american_to_implied(-110), 4)
    0.5238
    >>> round(american_to_implied(150), 4)
    0.4
    """
    return 1 / american_to_decimal(odds)


def decimal_to_american(decimal_odds: float) -> int:
    """
    Convert decimal odds back to American odds (rounded to nearest integer).

    Examples
    --------
    >>> decimal_to_american(2.5)
    150
    >>> decimal_to_american(1.909)
    -110
    """
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


# ---------------------------------------------------------------------------
# Vig / overround
# ---------------------------------------------------------------------------

def overround(implied_probs: list) -> float:
    """
    Calculate the bookmaker overround (sum of implied probs).

    A fair market sums to 1.0. Any excess above 1.0 is the vig.
    E.g. overround of 1.045 = 4.5% book margin.

    Parameters
    ----------
    implied_probs : list of float

    Returns
    -------
    float
        Sum of implied probabilities (>= 1.0 for a vig-inclusive market).

    Examples
    --------
    >>> round(overround([american_to_implied(-110), american_to_implied(-110)]), 4)
    1.0476
    """
    return sum(implied_probs)


def vig_percentage(implied_probs: list) -> float:
    """
    Return the vig as a percentage of the total handle implied by the odds.

    vig% = (overround - 1) / overround

    Examples
    --------
    >>> probs = [american_to_implied(-110), american_to_implied(-110)]
    >>> round(vig_percentage(probs), 4)
    0.0454
    """
    og = overround(implied_probs)
    return (og - 1) / og


def remove_vig(implied_probs: list) -> list:
    """
    Remove the bookmaker vig using the multiplicative method.

    Divides each implied probability by the overround so that the
    resulting probabilities sum to exactly 1.0.

    Parameters
    ----------
    implied_probs : list of float
        Raw implied probabilities (with vig).

    Returns
    -------
    list of float
        True (no-vig) probabilities that sum to 1.0.

    Examples
    --------
    >>> probs = [american_to_implied(-110), american_to_implied(-110)]
    >>> no_vig = remove_vig(probs)
    >>> [round(p, 4) for p in no_vig]
    [0.5, 0.5]
    >>> sum(no_vig)
    1.0
    """
    og = overround(implied_probs)
    return [p / og for p in implied_probs]


# ---------------------------------------------------------------------------
# Market-level helpers
# ---------------------------------------------------------------------------

def no_vig_market(american_odds: list, outcome_names: list = None) -> dict:
    """
    Take a list of American odds for one market (2-way or 3-way) and return
    a dict with implied probs, overround, vig%, and true no-vig probabilities.

    Parameters
    ----------
    american_odds : list of int
        e.g. [-110, -110] or [210, 330, 130]
    outcome_names : list of str, optional
        Labels for each outcome. Defaults to ["outcome_0", "outcome_1", ...].

    Returns
    -------
    dict with keys:
        outcomes        : list of str
        american_odds   : list of int
        implied_probs   : list of float   (with vig)
        overround       : float
        vig_pct         : float
        no_vig_probs    : list of float   (sum to 1.0)
        no_vig_american : list of int     (fair odds)

    Examples
    --------
    >>> result = no_vig_market([-110, -110], ["Home", "Away"])
    >>> result["no_vig_probs"]
    [0.5, 0.5]
    >>> result["overround"]
    1.0476190476190477

    >>> # Soccer 1X2
    >>> result = no_vig_market([210, 330, 130], ["Home", "Draw", "Away"])
    >>> [round(p, 4) for p in result["no_vig_probs"]]
    [0.3031, 0.2062, 0.4907]
    """
    n = len(american_odds)
    if outcome_names is None:
        outcome_names = [f"outcome_{i}" for i in range(n)]

    implied = [american_to_implied(o) for o in american_odds]
    og = overround(implied)
    vig_pct = vig_percentage(implied)
    true_probs = remove_vig(implied)
    fair_american = [decimal_to_american(1 / p) for p in true_probs]

    return {
        "outcomes": outcome_names,
        "american_odds": american_odds,
        "implied_probs": implied,
        "overround": og,
        "vig_pct": vig_pct,
        "no_vig_probs": true_probs,
        "no_vig_american": fair_american,
    }


# ---------------------------------------------------------------------------
# Multi-book sharpness selection
# ---------------------------------------------------------------------------

def sharpest_no_vig(
    book_odds: dict,
    outcome_names: list = None,
) -> dict:
    """
    Given odds from multiple books for the same market, identify the
    sharpest book (lowest vig) and return its no-vig probabilities as
    the best estimate of true outcome probability.

    The book with the lowest vig has moved its line closest to fair value
    and is generally considered the sharpest signal.

    Parameters
    ----------
    book_odds : dict
        {bookmaker_name: [american_odds_per_outcome]}
        e.g. {
            "draftkings": [-108, -112],
            "fanduel":    [-110, -110],
            "betmgm":     [-115, -105],
        }
    outcome_names : list of str, optional
        Labels for each outcome.

    Returns
    -------
    dict with keys:
        sharpest_book   : str
        sharpest_vig    : float
        all_vigs        : dict {bookmaker: vig_pct}
        no_vig_probs    : list of float  (from sharpest book)
        no_vig_american : list of int
        outcomes        : list of str

    Examples
    --------
    >>> book_odds = {
    ...     "draftkings": [-108, -112],
    ...     "fanduel":    [-110, -110],
    ...     "betmgm":     [-115, -105],
    ... }
    >>> result = sharpest_no_vig(book_odds, ["Home", "Away"])
    >>> result["sharpest_book"]
    'draftkings'
    >>> [round(p, 4) for p in result["no_vig_probs"]]
    [0.4865, 0.5135]
    """
    all_vigs = {}
    for book, odds in book_odds.items():
        implied = [american_to_implied(o) for o in odds]
        all_vigs[book] = vig_percentage(implied)

    sharpest_book = min(all_vigs, key=all_vigs.get)
    sharp_odds = book_odds[sharpest_book]
    market = no_vig_market(sharp_odds, outcome_names)

    return {
        "sharpest_book": sharpest_book,
        "sharpest_vig": all_vigs[sharpest_book],
        "all_vigs": all_vigs,
        "no_vig_probs": market["no_vig_probs"],
        "no_vig_american": market["no_vig_american"],
        "outcomes": market["outcomes"],
    }


def no_vig_from_df(df, game_id: str, market: str, outcome_names: list = None) -> dict:
    """
    Convenience wrapper: pull odds for a specific game+market from the
    odds DataFrame returned by odds_fetcher.get_odds_df() and compute
    the sharpest no-vig probabilities.

    Parameters
    ----------
    df : pd.DataFrame
        Output of get_odds_df().
    game_id : str
    market : str
        "h2h", "spreads", or "totals"
    outcome_names : list of str, optional

    Returns
    -------
    dict (same structure as sharpest_no_vig)
    """
    subset = df[(df["game_id"] == game_id) & (df["market"] == market)]
    if subset.empty:
        raise ValueError(f"No rows found for game_id={game_id!r} market={market!r}")

    book_odds = {}
    for book, grp in subset.groupby("bookmaker"):
        odds = grp.sort_values("outcome_name")["price"].astype(int).tolist()
        if len(odds) >= 2:
            book_odds[book] = odds

    if not book_odds:
        raise ValueError("Not enough bookmaker data to compute no-vig probabilities.")

    names = outcome_names or sorted(subset["outcome_name"].unique())
    return sharpest_no_vig(book_odds, outcome_names=names)


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("2-WAY MARKET: NBA moneyline (-110 / -110)")
    print("=" * 60)
    result = no_vig_market([-110, -110], ["Home", "Away"])
    for k, v in result.items():
        print(f"  {k}: {v}")

    print()
    print("=" * 60)
    print("2-WAY MARKET: NHL moneyline (-155 / +130)")
    print("=" * 60)
    result = no_vig_market([-155, 130], ["Favorite", "Underdog"])
    for k, v in result.items():
        if isinstance(v, list):
            print(f"  {k}: {[round(x, 4) if isinstance(x, float) else x for x in v]}")
        elif isinstance(v, float):
            print(f"  {k}: {round(v, 4)}")
        else:
            print(f"  {k}: {v}")

    print()
    print("=" * 60)
    print("3-WAY MARKET: Soccer 1X2 (+210 / +330 / +130)")
    print("=" * 60)
    result = no_vig_market([210, 330, 130], ["Home", "Draw", "Away"])
    for k, v in result.items():
        if isinstance(v, list):
            print(f"  {k}: {[round(x, 4) if isinstance(x, float) else x for x in v]}")
        elif isinstance(v, float):
            print(f"  {k}: {round(v, 4)}")
        else:
            print(f"  {k}: {v}")

    print()
    print("=" * 60)
    print("MULTI-BOOK: Sharpest no-vig (NBA moneyline)")
    print("=" * 60)
    book_odds = {
        "draftkings": [-108, -112],
        "fanduel":    [-110, -110],
        "betmgm":     [-115, -105],
        "caesars":    [-112, -108],
    }
    result = sharpest_no_vig(book_odds, ["Home", "Away"])
    print(f"  All vigs:       { {k: round(v*100, 3) for k, v in result['all_vigs'].items()} }%")
    print(f"  Sharpest book:  {result['sharpest_book']}  (vig={round(result['sharpest_vig']*100, 3)}%)")
    print(f"  No-vig probs:   {[round(p, 4) for p in result['no_vig_probs']]}")
    print(f"  Fair odds:      {result['no_vig_american']}")
