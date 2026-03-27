"""
Fetch odds data from the Odds API.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"


def get_sports():
    url = f"{BASE_URL}/sports"
    resp = requests.get(url, params={"apiKey": API_KEY})
    resp.raise_for_status()
    return resp.json()


def get_odds(sport: str, regions: str = "us", markets: str = "h2h"):
    url = f"{BASE_URL}/sports/{sport}/odds"
    params = {
        "apiKey": API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "american",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    sports = get_sports()
    print(f"Available sports: {[s['key'] for s in sports if not s['has_outrights']]}")
