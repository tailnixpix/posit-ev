"""
odds_fetcher.py — Fetch odds from The Odds API and return clean pandas DataFrames.

Supports: h2h, spreads, totals, player_props
Bookmakers: draftkings, fanduel, betmgm, pointsbet, caesars
"""
import sys
import os
import time
import logging
from typing import Optional, Union
import requests
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import ODDS_API_KEY, ODDS_API_BASE_URL, LOG_LEVEL

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SPORT_KEYS = [
    "icehockey_nhl",
    "basketball_nba",
    "basketball_ncaab",
    "baseball_mlb",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_usa_mls",
]

SPORTSBOOK_BOOKMAKERS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "pointsbet",
    "caesars",
    "betfair_ex_uk",   # Betfair Exchange — lowest vig (~2%), gold-standard sharp reference
]

# Prediction markets: regulated exchanges with very low vig.
# Available in all US states (Kalshi/Polymarket are federally regulated;
# NoVig is an exchange-style book). They only offer h2h (moneyline) markets.
PREDICTION_MARKET_BOOKMAKERS = [
    "kalshi",
    "novig",
    "polymarket",
    "prophetx",
]

# Combined list — prediction markets are included in h2h fetches.
# For spreads/totals they have no data and simply don't appear in results.
BOOKMAKERS = SPORTSBOOK_BOOKMAKERS  # backward-compat alias (sportsbooks only)
ALL_BOOKMAKERS = SPORTSBOOK_BOOKMAKERS + PREDICTION_MARKET_BOOKMAKERS

# Props use sportsbooks only — Betfair Exchange doesn't offer US player props
# and including it causes 422 errors that silently drop entire event responses.
PROPS_BOOKMAKERS = [b for b in SPORTSBOOK_BOOKMAKERS if b != "betfair_ex_uk"]

# Maps each bookmaker key to its source type
BOOKMAKER_SOURCE_TYPE: dict = {
    **{b: "sportsbook"         for b in SPORTSBOOK_BOOKMAKERS},
    **{b: "prediction_market"  for b in PREDICTION_MARKET_BOOKMAKERS},
}

MARKETS = ["h2h", "spreads", "totals"]
PROP_MARKETS = ["player_props"]  # fetched separately (event-level endpoint)

# Sports that support player prop fetching via event-level endpoint
PROP_SPORTS = ["basketball_nba", "baseball_mlb", "icehockey_nhl"]

# Prop market keys per sport (Odds API event-level endpoint)
PROP_MARKETS_BY_SPORT: dict = {
    "basketball_nba": [
        "player_points", "player_rebounds", "player_assists",
        "player_threes", "player_blocks", "player_steals",
    ],
    "baseball_mlb": [
        "batter_home_runs", "batter_hits", "batter_rbis",
        "pitcher_strikeouts",
    ],
    "icehockey_nhl": [
        "player_points", "player_goals", "player_assists",
        "player_shots_on_goal", "player_blocked_shots",
    ],
}

REGIONS = "us"
ODDS_FORMAT = "american"

# The free tier allows ~500 requests/month; respect a small delay between calls.
REQUEST_DELAY_SEC = 1.0

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict, retries: int = 3) -> Optional[Union[dict, list]]:
    """GET with retry/back-off. Returns parsed JSON or None on failure."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)
            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            if remaining is not None:
                log.debug("API quota — used: %s  remaining: %s", used, remaining)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                log.warning("Rate limited. Waiting %ds before retry %d/%d.", wait, attempt, retries)
                time.sleep(wait)
                continue

            if resp.status_code == 422:
                log.warning("Unprocessable request (likely unsupported market): %s", url)
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.Timeout:
            log.warning("Timeout on attempt %d/%d: %s", attempt, retries, url)
        except requests.exceptions.RequestException as exc:
            log.error("Request error on attempt %d/%d: %s", attempt, retries, exc)

        if attempt < retries:
            time.sleep(2 ** attempt)

    log.error("All %d attempts failed for: %s", retries, url)
    return None


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

def fetch_odds(
    sport_key: str,
    markets: list[str] = None,
    bookmakers: list[str] = None,
    regions: str = REGIONS,
) -> list[dict]:
    """Fetch game-level odds for one sport and one or more markets."""
    markets = markets or MARKETS
    bookmakers = bookmakers or BOOKMAKERS

    url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": regions,
        "markets": ",".join(markets),
        "bookmakers": ",".join(bookmakers),
        "oddsFormat": ODDS_FORMAT,
    }
    log.info("Fetching %s | markets: %s", sport_key, markets)
    data = _get(url, params)
    time.sleep(REQUEST_DELAY_SEC)
    return data or []


def fetch_player_props(sport_key: str, event_id: str, bookmakers: list[str] = None) -> list[dict]:
    """Fetch player prop markets for a single event using sport-specific markets.
    Props use sportsbooks only — Betfair Exchange doesn't offer US player props."""
    prop_markets = PROP_MARKETS_BY_SPORT.get(sport_key, [])
    if not prop_markets:
        return []
    bookmakers = bookmakers or PROPS_BOOKMAKERS
    url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": ",".join(prop_markets),
        "bookmakers": ",".join(bookmakers),
        "oddsFormat": ODDS_FORMAT,
    }
    log.debug("Fetching props for event %s (%s)", event_id, sport_key)
    data = _get(url, params)
    time.sleep(REQUEST_DELAY_SEC)
    return data or []


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_game_markets(game: dict) -> list[dict]:
    """Flatten one game's bookmaker/market/outcome data into a list of rows."""
    rows = []
    base = {
        "game_id": game.get("id"),
        "sport_key": game.get("sport_key"),
        "sport_title": game.get("sport_title"),
        "home_team": game.get("home_team"),
        "away_team": game.get("away_team"),
        "commence_time": game.get("commence_time"),
    }
    for bookie in game.get("bookmakers", []):
        for market in bookie.get("markets", []):
            for outcome in market.get("outcomes", []):
                row = {
                    **base,
                    "bookmaker": bookie["key"],
                    "market": market["key"],
                    "last_update": market.get("last_update"),
                    "outcome_name": outcome.get("name"),
                    "price": outcome.get("price"),
                    "point": outcome.get("point"),  # spreads / totals only
                }
                rows.append(row)
    return rows


def _parse_props(event_odds: dict) -> list[dict]:
    """Flatten event-level player prop data into rows."""
    rows = []
    base = {
        "game_id": event_odds.get("id"),
        "sport_key": event_odds.get("sport_key"),
        "home_team": event_odds.get("home_team"),
        "away_team": event_odds.get("away_team"),
        "commence_time": event_odds.get("commence_time"),
    }
    for bookie in event_odds.get("bookmakers", []):
        for market in bookie.get("markets", []):
            for outcome in market.get("outcomes", []):
                row = {
                    **base,
                    "bookmaker": bookie["key"],
                    "prop_market": market["key"],
                    "last_update": market.get("last_update"),
                    "player": outcome.get("description", outcome.get("name")),
                    "outcome_name": outcome.get("name"),
                    "price": outcome.get("price"),
                    "point": outcome.get("point"),
                }
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Public API — returns DataFrames
# ---------------------------------------------------------------------------

def get_odds_df(
    sport_keys: list[str] = None,
    markets: list[str] = None,
    bookmakers: list[str] = None,
) -> pd.DataFrame:
    """
    Fetch h2h / spreads / totals for all configured sports.
    Prediction market bookmakers (Kalshi, NoVig, Polymarket) are included
    automatically for h2h markets — they have no spread/total lines so they
    simply don't appear for those markets.

    Returns a tidy DataFrame with one row per (game, bookmaker, market, outcome).
    Includes a ``source_type`` column: "sportsbook" or "prediction_market".
    """
    sport_keys = sport_keys or SPORT_KEYS
    markets = markets or MARKETS
    # Use ALL_BOOKMAKERS so prediction markets are included in h2h fetches
    bookmakers = bookmakers or ALL_BOOKMAKERS
    all_rows = []

    for sport in sport_keys:
        games = fetch_odds(sport, markets=markets, bookmakers=bookmakers)
        for game in games:
            all_rows.extend(_parse_game_markets(game))

    if not all_rows:
        log.warning("No odds data returned.")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True)
    df["last_update"] = pd.to_datetime(df["last_update"], utc=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["point"] = pd.to_numeric(df["point"], errors="coerce")

    # Tag each row with its source type (sportsbook vs prediction_market)
    df["source_type"] = df["bookmaker"].map(BOOKMAKER_SOURCE_TYPE).fillna("sportsbook")

    # Drop any game that has already started — live odds skew EV artificially
    now = pd.Timestamp.now(tz="UTC")
    before = len(df)
    df = df[df["commence_time"] > now]
    dropped = before - len(df)
    if dropped:
        log.info("Filtered out %d rows belonging to live/started game(s).", dropped)

    return df


def get_props_df(
    sport_keys: list[str] = None,
    bookmakers: list[str] = None,
    max_games: int = 4,
) -> pd.DataFrame:
    """
    Fetch player props for NBA, MLB, and NHL only.
    Returns a tidy DataFrame with one row per (game, bookmaker, prop_market, player, outcome).

    Uses sportsbooks only — prediction markets don't offer player lines.
    One API request per event, so limited to max_games per sport to conserve quota.
    """
    sport_keys = [s for s in (sport_keys or PROP_SPORTS) if s in PROP_SPORTS]
    bookmakers = bookmakers or PROPS_BOOKMAKERS
    all_rows = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for sport in sport_keys:
        # Lightweight call — just need event IDs and commence times
        games = fetch_odds(sport, markets=["h2h"], bookmakers=["draftkings"])
        upcoming = sorted(
            [g for g in games if g.get("commence_time", "") >= now_iso],
            key=lambda g: g.get("commence_time", "")
        )[:max_games]

        for game in upcoming:
            event_data = fetch_player_props(sport, game["id"], bookmakers=bookmakers)
            if isinstance(event_data, dict) and event_data:
                all_rows.extend(_parse_props(event_data))
            elif isinstance(event_data, list):
                for item in event_data:
                    if isinstance(item, dict):
                        all_rows.extend(_parse_props(item))

    if not all_rows:
        log.debug("No player props data returned.")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["point"] = pd.to_numeric(df["point"], errors="coerce")
    df["source_type"] = df["bookmaker"].map(BOOKMAKER_SOURCE_TYPE).fillna("sportsbook")

    # Drop started games
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["commence_time"] > now]
    return df


def get_best_lines(df: pd.DataFrame, market: str = "h2h") -> pd.DataFrame:
    """
    Given the full odds DataFrame, return the best available line per
    (game, outcome) across all bookmakers for a given market.
    """
    subset = df[df["market"] == market].copy()
    if subset.empty:
        return subset

    # Best moneyline = highest price for a given outcome
    idx = subset.groupby(["game_id", "outcome_name"])["price"].idxmax()
    return subset.loc[idx].reset_index(drop=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch sports odds into a DataFrame.")
    parser.add_argument("--sport", nargs="+", default=SPORT_KEYS, help="Sport key(s)")
    parser.add_argument("--market", nargs="+", default=MARKETS, help="Market(s): h2h spreads totals")
    parser.add_argument("--props", action="store_true", help="Fetch player props instead")
    parser.add_argument("--save", action="store_true", help="Save output to data/")
    args = parser.parse_args()

    if args.props:
        df = get_props_df(sport_keys=args.sport)
        label = "props"
    else:
        df = get_odds_df(sport_keys=args.sport, markets=args.market)
        label = "odds"

    if df.empty:
        print("No data returned.")
        sys.exit(0)

    print(f"\n--- {label.upper()} SAMPLE ---")
    print(df.head(20).to_string(index=False))
    print(f"\nShape: {df.shape}")
    print(f"Sports: {df['sport_key'].unique()}")
    if "market" in df.columns:
        print(f"Markets: {df['market'].unique()}")
    if "bookmaker" in df.columns:
        print(f"Bookmakers: {df['bookmaker'].unique()}")

    if args.save:
        os.makedirs("data", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"data/{label}_{ts}.csv"
        df.to_csv(path, index=False)
        print(f"\nSaved to {path}")
