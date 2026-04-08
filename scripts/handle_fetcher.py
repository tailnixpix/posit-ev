"""
scripts/handle_fetcher.py — Public betting handle & sharp money data.

Source: Action Network (unofficial public API — no auth required).
Provides bet% and money% (handle%) for major US sports.

Sharp Money Signal
------------------
When money% >> bet% on a side, that means larger bettors (sharps /
professional money) are on that side relative to the ticket count.
Combined with positive CLV (line moved in your favour at open) this
is the strongest publicly-available signal of professional backing.

Usage
-----
    from scripts.handle_fetcher import fetch_handle_for_game

    bet_pct, money_pct = fetch_handle_for_game(
        game   = "Miami Heat @ Boston Celtics",
        market = "h2h",
        team   = "Boston Celtics",
        league = "basketball_nba",
    )
    # → (35.0, 65.0)  means 35% of tickets, 65% of money → sharp side
"""

import logging
from datetime import date
from typing import Optional, Tuple

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE    = "https://api.actionnetwork.com/web/v1"
_TIMEOUT = 12

# Maps our Odds-API sport keys → Action Network sport slugs
_SPORT_MAP: dict = {
    "basketball_nba":       "nba",
    "baseball_mlb":         "mlb",
    "icehockey_nhl":        "nhl",
    "americanfootball_nfl": "nfl",

}

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0 Safari/537.36"
    ),
    "Accept":   "application/json",
    "Referer":  "https://www.actionnetwork.com/",
    "Origin":   "https://www.actionnetwork.com",
})

# In-memory cache: (sport_key, date_str) → parsed game list
_GAME_CACHE: dict = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Lowercase, strip — for fuzzy matching."""
    return " ".join(text.lower().split())


def _teams_overlap(a: str, b: str) -> bool:
    """
    True when at least one significant word is shared between two team names.
    e.g. "Boston Celtics" matches "Celtics", "Boston", "Boston Celtics Inc."
    """
    skip = {"at", "the", "a", "an", "vs", "fc", "sc", "city", "state"}
    wa = {w for w in _slug(a).split() if w not in skip and len(w) > 2}
    wb = {w for w in _slug(b).split() if w not in skip and len(w) > 2}
    return bool(wa & wb)


def _extract_team_names(game_obj: dict) -> Tuple[str, str]:
    """
    Return (away_team_name, home_team_name) from an Action Network game object.
    Handles multiple response shapes across API versions.
    """
    # Try nested home_team / away_team objects first
    def _name(obj: dict) -> str:
        return (
            obj.get("full_name")
            or obj.get("display_name")
            or obj.get("name")
            or ""
        )

    if "home_team" in game_obj and "away_team" in game_obj:
        return _name(game_obj["away_team"]), _name(game_obj["home_team"])

    # Fall back to teams array matched by home/away ID
    teams      = game_obj.get("teams", [])
    home_id    = game_obj.get("home_team_id") or game_obj.get("home_id")
    away_id    = game_obj.get("away_team_id") or game_obj.get("away_id")
    home_name  = ""
    away_name  = ""
    for t in teams:
        n = _name(t)
        if t.get("id") == home_id:
            home_name = n
        if t.get("id") == away_id:
            away_name = n

    return away_name, home_name


def _consensus_line(odds_list: list) -> Optional[dict]:
    """
    Pick the consensus / aggregate odds object.
    Action Network uses book_id=15 for their consensus composite.
    Falls back to book_id=0 or the first entry.
    """
    for o in odds_list:
        if o.get("book_id") in (15, "15"):
            return o
    for o in odds_list:
        bname = str(o.get("book_name", "") or o.get("type", "")).lower()
        if "consensus" in bname or "composite" in bname:
            return o
    for o in odds_list:
        if o.get("book_id") in (0, "0"):
            return o
    return odds_list[0] if odds_list else None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fetch_an_games(sport_key: str, game_date: Optional[str] = None) -> list:
    """
    Fetch and cache Action Network game data for one sport + date.
    Returns parsed list of dicts with keys: away_team, home_team, odds.
    """
    an_sport = _SPORT_MAP.get(sport_key)
    if not an_sport:
        return []

    today     = game_date or date.today().isoformat()
    cache_key = (sport_key, today)
    if cache_key in _GAME_CACHE:
        return _GAME_CACHE[cache_key]

    try:
        r = _SESSION.get(
            f"{_BASE}/games",
            params={"sport": an_sport, "date": today},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Action Network unavailable (%s, %s): %s", an_sport, today, exc)
        _GAME_CACHE[cache_key] = []
        return []

    parsed = []
    for g in data.get("games", []):
        away, home = _extract_team_names(g)
        if not away or not home:
            continue
        parsed.append({"away_team": away, "home_team": home, "odds": g.get("odds", [])})

    log.debug(
        "Action Network: %d games loaded for %s on %s", len(parsed), an_sport, today
    )
    _GAME_CACHE[cache_key] = parsed
    return parsed


def fetch_handle_for_game(
    game:       str,
    market:     str,
    team:       str,
    league:     str,
    game_date:  Optional[str] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (bet_pct, money_pct) for one outcome in a game.

    bet_pct   — percentage of total bets placed on this side (0–100)
    money_pct — percentage of total money wagered on this side (0–100)

    Returns (None, None) when data is unavailable.

    Parameters
    ----------
    game      : "Away @ Home" string (from EVBetCache.game)
    market    : "h2h" | "spreads" | "totals"
    team      : outcome label — team name, "Over X.X", or "Under X.X"
    league    : Odds API sport key, e.g. "basketball_nba"
    game_date : ISO date string for the game; defaults to today
    """
    if not game or " @ " not in game:
        return None, None

    an_games = _fetch_an_games(league, game_date)
    if not an_games:
        return None, None

    parts    = game.split(" @ ", 1)
    our_away = parts[0].strip()
    our_home = parts[1].strip()

    matched = next(
        (
            g for g in an_games
            if _teams_overlap(g["away_team"], our_away)
            and _teams_overlap(g["home_team"], our_home)
        ),
        None,
    )
    if not matched:
        log.debug("No Action Network match for: %s", game)
        return None, None

    consensus = _consensus_line(matched["odds"])
    if not consensus:
        return None, None

    team_lower = team.lower()
    is_over    = team_lower.startswith("over")
    is_under   = team_lower.startswith("under")
    is_home    = (not is_over and not is_under and _teams_overlap(our_home, team))

    if market in ("h2h", "spreads"):
        side = "home" if is_home else "away"
        bet_pct   = _safe_float(
            consensus.get(f"{side}_bet_pct")
            or consensus.get(f"{side}_bets_pct")
        )
        money_pct = _safe_float(
            consensus.get(f"{side}_money_pct")
            or consensus.get(f"{side}_handle_pct")
        )
    elif market == "totals":
        side = "over" if is_over else "under"
        bet_pct   = _safe_float(
            consensus.get(f"{side}_bet_pct")
            or consensus.get(f"{side}_bets_pct")
        )
        money_pct = _safe_float(
            consensus.get(f"{side}_money_pct")
            or consensus.get(f"{side}_handle_pct")
        )
    else:
        return None, None

    return bet_pct, money_pct


def compute_sharp_score(
    bet_pct:      Optional[float],
    money_pct:    Optional[float],
    opening_odds: Optional[int],
    current_odds: Optional[int],
) -> Optional[float]:
    """
    Return a 0–100 sharp money score.

    Factors
    -------
    1. Money% vs Bet% divergence  (+40 pts max when money >> tickets)
    2. CLV direction              (+20 pts if line moved in your favour)

    Thresholds (used for badges in the UI):
        ≥ 65  → 🔥 Sharp Money  (professional money on this side)
        ≤ 35  → 📢 Public Lean  (ticket count dominated by public; faded by $)
        36–64 → neutral
    """
    if bet_pct is None and money_pct is None:
        return None

    score = 50.0  # baseline: neutral

    # --- Factor 1: Money vs Ticket divergence ---
    if bet_pct is not None and money_pct is not None:
        divergence = money_pct - bet_pct          # positive = bigger bettors on this side
        score += min(40.0, max(-40.0, divergence * 1.6))

    # --- Factor 2: CLV direction ---
    if opening_odds is not None and current_odds is not None and opening_odds != current_odds:
        def _to_prob(o: int) -> float:
            return abs(o) / (abs(o) + 100) if o < 0 else 100 / (o + 100)

        open_prob = _to_prob(opening_odds)
        curr_prob = _to_prob(current_odds)
        # Line moving shorter (prob goes up) = market betting more on this side
        # That can mean sharp money came in (steam) → bonus
        if curr_prob > open_prob:     # line got shorter = sharp steam in
            score += 20.0
        else:                          # line got longer  = money left / fade
            score -= 10.0

    return round(max(0.0, min(100.0, score)), 1)
