"""
Fetch odds data from the Odds API.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests
from config import (
    ODDS_API_KEY,
    ODDS_API_BASE_URL,
    LEAGUES,
    DEFAULT_REGION,
    DEFAULT_MARKET,
    DEFAULT_ODDS_FORMAT,
)


def get_sports():
    url = f"{ODDS_API_BASE_URL}/sports"
    resp = requests.get(url, params={"apiKey": ODDS_API_KEY})
    resp.raise_for_status()
    return resp.json()


def get_odds(league_key: str, regions: str = DEFAULT_REGION, markets: str = DEFAULT_MARKET):
    url = f"{ODDS_API_BASE_URL}/sports/{league_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": DEFAULT_ODDS_FORMAT,
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    league = sys.argv[1] if len(sys.argv) > 1 else "NBA"
    key = LEAGUES.get(league)
    if not key:
        print(f"Unknown league '{league}'. Available: {list(LEAGUES.keys())}")
        sys.exit(1)

    print(f"Fetching odds for {league} ({key})...")
    games = get_odds(key)
    print(f"Found {len(games)} games.")
    for game in games[:3]:
        print(f"  {game['home_team']} vs {game['away_team']} — {game['commence_time']}")
