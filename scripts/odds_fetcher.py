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
from datetime import datetime, timedelta, timezone

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
    "soccer_uefa_champs_league",
    "soccer_fifa_world_cup",
]

# Championship / outright futures — separate Odds API sport keys.
# These are only active during their respective playoff seasons.
# They use the "outrights" market and have commence_times months away
# (Stanley Cup Finals, NBA Finals), so they bypass the 7-day window filter.
FUTURES_SPORT_KEYS = [
    "icehockey_nhl_championship_winner",
    "basketball_nba_championship_winner",
]

# Friendly name for each futures key — shown as the "game" label on bet cards.
FUTURES_LABELS: dict = {
    "icehockey_nhl_championship_winner": "NHL Championship Winner",
    "basketball_nba_championship_winner": "NBA Championship Winner",
}

SPORTSBOOK_BOOKMAKERS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "pointsbet",
    "caesars",
    "betfair_ex_uk",   # Betfair Exchange — lowest vig (~2%), gold-standard sharp reference
    "pinnacle",        # Sharpest sportsbook globally — included for true-prob anchoring in game markets
]

# Prediction markets: federally regulated contract exchanges (CFTC / commodity law).
# Available in all 50 states. Only offer h2h markets.
PREDICTION_MARKET_BOOKMAKERS = [
    "kalshi",      # CFTC-regulated, ~0% vig
    "polymarket",  # Global prediction exchange, ~1% vig
]

# Betting exchanges: peer-to-peer platforms where users bet against each other.
# No traditional house edge — commission only (~1-2%). Only h2h markets.
EXCHANGE_BOOKMAKERS = [
    "novig",       # P2P exchange, select US states, ~2% commission
    "prophetx",    # Sports prediction exchange, all 50 states, ~0% commission
    "betopenly",   # P2P betting exchange, ~1% commission
]

# Combined list — prediction markets and exchanges are included in h2h fetches.
# For spreads/totals they have no data and simply don't appear in results.
BOOKMAKERS = SPORTSBOOK_BOOKMAKERS  # backward-compat alias (sportsbooks only)
ALL_BOOKMAKERS = SPORTSBOOK_BOOKMAKERS + PREDICTION_MARKET_BOOKMAKERS + EXCHANGE_BOOKMAKERS

# Additional sportsbooks used only for player-prop fetches.
# DraftKings, FanDuel, BetMGM, and Caesars do NOT offer batter/pitcher props
# through the Odds API — these books do.  They surface as actionable bets
# in prop cards (source_type = "sportsbook").
PROP_EXTRA_SPORTSBOOKS = [
    "betrivers",       # Major US sportsbook — broad prop coverage
    "williamhill_us",  # William Hill US — HR/pitcher props
    "espnbet",         # ESPN Bet — wide US market
    "hardrockbet",     # Hard Rock Bet — US sportsbook
    "betonlineag",     # Offshore with strong prop lines
]

# Sharp reference books included in the props pool but NOT surfaced as bets
# (source_type = "exchange" so EV calculator uses them for true-prob only).
PROP_SHARP_REFERENCE_BOOKS = [
    "pinnacle",        # Sharpest book globally — excellent true-prob anchor for props
]

# Props fetch: all sportsbooks that carry prop lines + sharp references.
# Novig IS used here so its sharp lines serve as the true-probability anchor and
# prevent sportsbook-vs-sportsbook EV signals that Novig's efficient market would
# not support.  Novig props are NOT surfaced as bets in the output — only its
# probability reference matters.  (See find_positive_ev_props in ev_calculator.py.)
#
# Books excluded from the SPORTSBOOK_BOOKMAKERS slice for props:
#   betfair_ex_uk — UK-only exchange, no US prop coverage
#   pinnacle      — added to SPORTSBOOK_BOOKMAKERS for game-level but already
#                   included via PROP_SHARP_REFERENCE_BOOKS as reference-only
_PROPS_SPORTSBOOK_EXCLUDE = {"betfair_ex_uk", "pinnacle"}
PROPS_BOOKMAKERS = (
    [b for b in SPORTSBOOK_BOOKMAKERS if b not in _PROPS_SPORTSBOOK_EXCLUDE]
    + PROP_EXTRA_SPORTSBOOKS
    + PROP_SHARP_REFERENCE_BOOKS   # Pinnacle as reference-only (source_type = "exchange")
    + ["novig"]
)

# Maps each bookmaker key to its source type
BOOKMAKER_SOURCE_TYPE: dict = {
    **{b: "sportsbook"         for b in SPORTSBOOK_BOOKMAKERS},
    **{b: "sportsbook"         for b in PROP_EXTRA_SPORTSBOOKS},
    **{b: "exchange"           for b in PROP_SHARP_REFERENCE_BOOKS},  # reference only, not surfaced
    **{b: "prediction_market"  for b in PREDICTION_MARKET_BOOKMAKERS},
    **{b: "exchange"           for b in EXCHANGE_BOOKMAKERS},
}

MARKETS = ["h2h", "spreads", "totals"]
PROP_MARKETS = ["player_props"]  # fetched separately (event-level endpoint)

# Game-level markets that are only valid for specific sports.
# These are merged into the fetch for the relevant sport only — bundling
# unsupported markets into the global request causes a 422 for the entire sport.
# NOTE: team_totals caused 422 across all sports (unsupported by prediction-market
# bookmakers in our list). NRFI/YRFI are NOT available via The Odds API — all
# variant keys (nrfi, first_inning_nrfi, game_nrfi, etc.) return 422.
_SOCCER_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_usa_mls",
    "soccer_uefa_champs_league",
    "soccer_fifa_world_cup",
]
# Soccer-specific markets:
#   h2h_3_way  — 3-way moneyline (Home / Draw / Away), standard for league soccer
#   btts       — Both Teams to Score (Yes / No)
# NOTE: soccer_fifa_world_cup does NOT support h2h_3_way or btts via the Odds API
# (returns 422 INVALID_MARKET, blocking the entire fetch). It only supports the
# base h2h/spreads/totals markets, so it is excluded from the extras dict.
_SOCCER_KEYS_WITH_EXTRAS = [k for k in _SOCCER_KEYS if k != "soccer_fifa_world_cup"]
SPORT_MARKETS_EXTRA: dict = {
    sport: ["h2h_3_way", "btts"]
    for sport in _SOCCER_KEYS_WITH_EXTRAS
}

# Sports that support player prop fetching via event-level endpoint
PROP_SPORTS = ["basketball_nba", "baseball_mlb", "icehockey_nhl"]

# Prop market keys per sport (Odds API event-level endpoint).
# Only include keys confirmed valid by The Odds API — invalid keys return a 422
# for the entire event request, wiping out all props for that game.
PROP_MARKETS_BY_SPORT: dict = {
    "basketball_nba": [
        "player_points", "player_rebounds", "player_assists",
        "player_threes", "player_blocks", "player_steals",
        "player_turnovers",       # turnovers over/under
        # player_double_double removed — The Odds API returns 422 for yes/no
        # special markets (unrecognised market type in the event-level endpoint).
    ],
    "baseball_mlb": [
        # Team markets (event-level, same fetch/parse path as props)
        "team_totals",            # per-team run total Over/Under
        # Batter props
        "batter_home_runs", "batter_hits", "batter_rbis",
        "batter_total_bases",     # total bases over/under
        "batter_strikeouts",      # batter strikeout yes/no
        # Pitcher props
        "pitcher_strikeouts", "pitcher_hits_allowed",
        "pitcher_earned_runs",
    ],
    "icehockey_nhl": [
        # player_anytime_goalscorer, player_power_play_points, and player_saves
        # removed — The Odds API returns 422 for these keys (unrecognised market
        # names in the event-level endpoint).
        "player_points", "player_goals", "player_assists",
        "player_shots_on_goal", "player_blocked_shots",
    ],
}

REGIONS = "us"
ODDS_FORMAT = "american"

# The free tier allows ~500 requests/month; respect a small delay between calls.
REQUEST_DELAY_SEC = 1.0

# Credit alert thresholds — send Telegram warning when remaining drops below these.
LOW_CREDIT_THRESHOLD      = 500   # yellow alert
CRITICAL_CREDIT_THRESHOLD = 100   # red alert

# ---------------------------------------------------------------------------
# API quota tracking (module-level, shared across all fetch calls in a run)
# ---------------------------------------------------------------------------

_QUOTA_STATE: dict = {
    "remaining": None,   # int — credits left as of last successful response header
    "used":      None,   # int — credits used so far
    "exhausted": False,  # True when OUT_OF_USAGE_CREDITS error is received
}


def get_quota_state() -> dict:
    """Return a snapshot of the current API quota state."""
    return dict(_QUOTA_STATE)


def reset_quota_state() -> None:
    """Reset exhausted flag at the start of a new pipeline run."""
    _QUOTA_STATE["exhausted"] = False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict, retries: int = 3) -> Optional[Union[dict, list]]:
    """GET with retry/back-off. Returns parsed JSON or None on failure."""
    # Short-circuit immediately if we already know credits are exhausted this run.
    if _QUOTA_STATE["exhausted"]:
        log.debug("Skipping request — quota exhausted: %s", url)
        return None

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)

            # Track quota from response headers on every call
            remaining = resp.headers.get("x-requests-remaining")
            used      = resp.headers.get("x-requests-used")
            if remaining is not None:
                try:
                    _QUOTA_STATE["remaining"] = int(remaining)
                    _QUOTA_STATE["used"]      = int(used) if used is not None else None
                except ValueError:
                    pass
                log.debug("API quota — used: %s  remaining: %s", used, remaining)

            # 401 — check for credit exhaustion vs. bad key
            if resp.status_code == 401:
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                if body.get("error_code") == "OUT_OF_USAGE_CREDITS":
                    _QUOTA_STATE["exhausted"] = True
                    log.critical(
                        "The Odds API quota exhausted (OUT_OF_USAGE_CREDITS). "
                        "All remaining fetch calls are aborted. "
                        "Renew credits at https://the-odds-api.com"
                    )
                else:
                    log.error(
                        "The Odds API returned 401 Unauthorized — check ODDS_API_KEY. "
                        "Body: %s", body
                    )
                return None

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
        log.warning("fetch_player_props: no prop markets configured for sport %s", sport_key)
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
    log.info("Props: fetching event %s (%s) — markets: %s", event_id, sport_key, prop_markets)
    data = _get(url, params)
    time.sleep(REQUEST_DELAY_SEC)
    if data is None:
        log.warning("Props: event %s (%s) returned no data (422/quota/timeout).", event_id, sport_key)
        return []
    return data  # _get() guarantees dict or list on success


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


def _parse_props(event_odds: dict, sport_key: str = None) -> list[dict]:
    """Flatten event-level player prop data into rows."""
    rows = []
    # The event-level endpoint doesn't echo sport_key in the response body,
    # so we accept it as an explicit argument and fall back to the field if present.
    base = {
        "game_id": event_odds.get("id"),
        "sport_key": sport_key or event_odds.get("sport_key"),
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
        # Merge base markets with any sport-specific extras (e.g. nrfi for MLB).
        # Keeping extras separate avoids 422 errors on sports that don't
        # recognise the market key.
        sport_markets = list(markets) + SPORT_MARKETS_EXTRA.get(sport, [])
        games = fetch_odds(sport, markets=sport_markets, bookmakers=bookmakers)
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

    # Drop games more than 7 days away — keeps cache focused on actionable bets
    # while allowing playoff series (scheduled 4-7 days out) to appear as soon
    # as bookmakers post lines.
    cutoff = now + pd.Timedelta(hours=168)
    before2 = len(df)
    df = df[df["commence_time"] <= cutoff]
    far_dropped = before2 - len(df)
    if far_dropped:
        log.info("Filtered out %d rows for games >7 days away.", far_dropped)

    # Drop quarter-point spread lines (x.25 / x.75) for soccer — only whole
    # and half-number lines are standard in soccer betting markets.
    # Asian handicap quarter-lines are not offered by US sportsbooks and
    # produce phantom EV signals against books that don't price them.
    soccer_spread_mask = (
        df["sport_key"].str.startswith("soccer_", na=False) &
        (df["market"] == "spreads") &
        df["point"].notna() &
        (df["point"] % 0.5 != 0)
    )
    qtr_dropped = soccer_spread_mask.sum()
    if qtr_dropped:
        df = df[~soccer_spread_mask]
        log.info("Filtered out %d quarter-point soccer spread rows.", qtr_dropped)

    return df


def get_props_df(
    sport_keys: list[str] = None,
    bookmakers: list[str] = None,
    max_games: int = 6,
) -> pd.DataFrame:
    """
    Fetch player props for NBA, MLB, and NHL only.
    Returns a tidy DataFrame with one row per (game, bookmaker, prop_market, player, outcome).

    Uses sportsbooks only — prediction markets don't offer player lines.
    One API request per event, so limited to max_games per sport to conserve quota.

    Game selection: takes up to max_games across the next 30 hours so the
    props pool spans a full day's afternoon AND evening slate plus next-day
    afternoon games, ensuring props are available even when the pipeline runs
    late in the evening (was 18 hours, extended to 30 for next-day coverage).
    """
    sport_keys = [s for s in (sport_keys or PROP_SPORTS) if s in PROP_SPORTS]
    bookmakers = bookmakers or PROPS_BOOKMAKERS
    all_rows = []
    now_utc    = datetime.now(timezone.utc)
    now_iso    = now_utc.isoformat()
    window_iso = (now_utc + timedelta(hours=30)).isoformat()

    for sport in sport_keys:
        # Lightweight call — just need event IDs and commence times
        games = fetch_odds(sport, markets=["h2h"], bookmakers=["draftkings"])
        # Take games starting within the next 30 hours, sorted by start time.
        # 30 hours (was 18h) ensures next-day afternoon games are included even
        # when the pipeline runs late in the evening — e.g. a 9pm run now covers
        # games up to 3am the following night, capturing a full day+half slate.
        upcoming = sorted(
            [g for g in games
             if now_iso <= g.get("commence_time", "") <= window_iso],
            key=lambda g: g.get("commence_time", "")
        )[:max_games]

        log.info(
            "Props: %s — %d total games from API, %d within 30h window",
            sport, len(games), len(upcoming),
        )

        for game in upcoming:
            game_label = f"{game.get('away_team', '?')} @ {game.get('home_team', '?')}"
            event_data = fetch_player_props(sport, game["id"], bookmakers=bookmakers)
            before = len(all_rows)
            if isinstance(event_data, dict) and event_data:
                all_rows.extend(_parse_props(event_data, sport_key=sport))
            elif isinstance(event_data, list):
                for item in event_data:
                    if isinstance(item, dict):
                        all_rows.extend(_parse_props(item, sport_key=sport))
            added = len(all_rows) - before
            log.info("Props: %s — %s parsed %d rows", sport, game_label, added)

    if not all_rows:
        log.warning("Props: no raw prop data returned across all sports (no games in window, quota issue, or all events returned 422).")
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


def get_futures_df(
    sport_keys: list[str] = None,
    bookmakers: list[str] = None,
) -> pd.DataFrame:
    """
    Fetch championship winner (outright/futures) odds for playoff sports.

    Unlike get_odds_df(), this function:
    - Uses the dedicated *_championship_winner sport keys
    - Fetches only the "outrights" market
    - Does NOT apply the 7-day commence_time filter (finals are months away)
    - Sets the "game" field to a friendly label (e.g. "NHL Championship Winner")
      since futures have no home_team / away_team

    Returns a tidy DataFrame compatible with find_all_positive_ev().
    Active only when the relevant sport keys are in the Odds API active list.
    """
    sport_keys = sport_keys or FUTURES_SPORT_KEYS
    bookmakers = bookmakers or ALL_BOOKMAKERS
    all_rows = []

    for sport in sport_keys:
        games = fetch_odds(sport, markets=["outrights"], bookmakers=bookmakers)
        for game in games:
            label = FUTURES_LABELS.get(sport, sport)
            for bookie in game.get("bookmakers", []):
                for market in bookie.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        all_rows.append({
                            "game_id":      game.get("id"),
                            "sport_key":    sport,
                            "sport_title":  game.get("sport_title", label),
                            "home_team":    label,      # use label since no team
                            "away_team":    label,
                            "game":         label,      # shown on bet cards
                            "commence_time": game.get("commence_time"),
                            "bookmaker":    bookie["key"],
                            "market":       "outrights",
                            "last_update":  market.get("last_update"),
                            "outcome_name": outcome.get("name"),
                            "price":        outcome.get("price"),
                            "point":        None,
                        })

    if not all_rows:
        log.info("get_futures_df: no futures data returned (playoffs may not be active).")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["commence_time"] = pd.to_datetime(df["commence_time"], utc=True, errors="coerce")
    df["last_update"]   = pd.to_datetime(df["last_update"],   utc=True, errors="coerce")
    df["price"]         = pd.to_numeric(df["price"], errors="coerce")
    df["source_type"]   = df["bookmaker"].map(BOOKMAKER_SOURCE_TYPE).fillna("sportsbook")

    # Deduplicate (bookmaker, game_id, outcome_name) — exchange platforms like
    # betfair_ex_uk list both back and lay markets as separate rows for the same
    # outcome, causing index misalignment in the EV calculator.  Keep the row with
    # the highest price (best available back odds for the bettor).
    df = (
        df.sort_values("price", ascending=False)
          .drop_duplicates(subset=["game_id", "bookmaker", "outcome_name"], keep="first")
          .reset_index(drop=True)
    )

    # Drop started futures (shouldn't happen, but guard anyway)
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["commence_time"] > now]

    log.info(
        "get_futures_df: %d futures rows across %d sport(s).",
        len(df), df["sport_key"].nunique() if not df.empty else 0,
    )
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
