"""
Expected Value (EV) calculator for sports betting.
"""


def american_to_decimal(american_odds: int) -> float:
    if american_odds > 0:
        return (american_odds / 100) + 1
    return (100 / abs(american_odds)) + 1


def implied_probability(american_odds: int) -> float:
    decimal = american_to_decimal(american_odds)
    return 1 / decimal


def remove_vig(probs: list[float]) -> list[float]:
    total = sum(probs)
    return [p / total for p in probs]


def expected_value(true_prob: float, american_odds: int, stake: float = 1.0) -> float:
    decimal = american_to_decimal(american_odds)
    profit = stake * (decimal - 1)
    loss = stake
    return (true_prob * profit) - ((1 - true_prob) * loss)


def find_positive_ev(market_odds: list[int], model_probs: list[float], stake: float = 100.0):
    results = []
    for odds, prob in zip(market_odds, model_probs):
        ev = expected_value(prob, odds, stake)
        results.append({
            "american_odds": odds,
            "model_prob": prob,
            "implied_prob": implied_probability(odds),
            "ev": round(ev, 2),
            "positive_ev": ev > 0,
        })
    return results


if __name__ == "__main__":
    # Example: two-outcome market
    market = [-110, -110]
    implied = [implied_probability(o) for o in market]
    fair = remove_vig(implied)

    print("Fair probabilities (vig removed):", [round(p, 4) for p in fair])
    results = find_positive_ev(market, fair)
    for r in results:
        print(r)
