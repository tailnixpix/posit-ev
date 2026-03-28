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
    "baseball_mlb",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_usa_mls",
]

BOOKMAKERS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "pointsbet",
    "caesars",
]

MARKETS = ["h2h", "spreads", "totals"]
PROP_MARKETS = ["player_props"]  # fetched separately (event-level endpoint)

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
    """Fetch player prop markets for a single event."""
    bookmakers = bookmakers or BOOKMAKERS
    url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": "player_points,player_rebounds,player_assists,player_threes,"
                   "player_points_alternate,player_blocks,player_steals,"
                   "player_shots_on_target,player_goals",
        "bookmakers": ",".join(bookmakers),
        "oddsFormat": ODDS_FORMAT,
    }
    log.debug("Fetching props for event %s", event_id)
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
    Returns a tidy DataFrame with one row per (game, bookmaker, market, outcome).
    """
    sport_keys = sport_keys or SPORT_KEYS
    markets = markets or MARKETS
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
    return df


def get_props_df(
    sport_keys: list[str] = None,
    bookmakers: list[str] = None,
    max_games: int = 5,
) -> pd.DataFrame:
    """
    Fetch player props for the next `max_games` upcoming games per sport.
    Returns a tidy DataFrame with one row per (game, bookmaker, prop market, player, outcome).

    Note: player_props use more API credits (one request per event).
    """
    sport_keys = sport_keys or SPORT_KEYS
    all_rows = []

    for sport in sport_keys:
        games = fetch_odds(sport, markets=["h2h"], bookmakers=bookmakers)  # lightweight call to get event IDs
        games_sorted = sorted(games, key=lambda g: g.get("commence_time", ""))
        upcoming = [
            g for g in games_sorted
            if g.get("commence_time", "") >= datetime.now(timezone.utc).isoformat()
        ][:max_games]

        for game in upcoming:
            event_data = fetch_player_props(sport, game["id"], bookmakers=bookmakers)
            if isinstance(event_data, dict):
                all_rows.extend(_parse_props(event_data))
            elif isinstance(event_data, list) and event_data:
                for item in event_data:
                    all_rows.extend(_parse_props(item))

    if not all_rows:
        log.warning("No player props data returned.")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True)
    df["last_update"] = pd.to_datetime(df["last_update"], utc=True)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["point"] = pd.to_numeric(df["point"], errors="coerce")
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
