"""
Central configuration: loads environment variables and defines Odds API league keys.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- API credentials ---
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORTS_DATA_API_KEY = os.getenv("SPORTS_DATA_API_KEY")

if not ODDS_API_KEY or ODDS_API_KEY == "your_odds_api_key_here":
    raise EnvironmentError("ODDS_API_KEY is not set. Add it to your .env file.")

# --- Odds API base URL ---
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# --- League keys (as defined by The Odds API) ---
LEAGUES = {
    # American Football
    "NFL": "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",

    # Basketball
    "NBA": "basketball_nba",
    "NCAAB": "basketball_ncaab",
    "WNBA": "basketball_wnba",
    "EuroLeague": "basketball_euroleague",

    # Baseball
    "MLB": "baseball_mlb",

    # Hockey
    "NHL": "icehockey_nhl",

    # Soccer
    "EPL": "soccer_epl",
    "MLS": "soccer_usa_mls",
    "La Liga": "soccer_spain_la_liga",
    "Bundesliga": "soccer_germany_bundesliga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
    "Champions League": "soccer_uefa_champs_league",
    "Europa League": "soccer_uefa_europa_league",

    # Tennis
    "ATP": "tennis_atp_french_open",
    "WTA": "tennis_wta_french_open",

    # MMA / Boxing
    "UFC/MMA": "mma_mixed_martial_arts",

    # Golf
    "PGA Tour": "golf_pga_championship",

    # Australian Rules
    "AFL": "aussierules_afl",

    # Rugby
    "Rugby Union": "rugbyleague_nrl",
}

# --- Default request settings ---
DEFAULT_REGION = "us"          # us | uk | eu | au
DEFAULT_MARKET = "h2h"         # h2h | spreads | totals
DEFAULT_ODDS_FORMAT = "american"  # american | decimal | fractional

# --- Timezone ---
from zoneinfo import ZoneInfo
LOCAL_TZ = ZoneInfo("America/Chicago")  # Central Time (CST/CDT)

# --- Misc ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
