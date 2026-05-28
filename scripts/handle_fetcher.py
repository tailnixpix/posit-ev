"""
scripts/handle_fetcher.py — Public betting consensus & sharp money signal.

Primary source: Sports Betting Dime JSON API (no auth required).
Provides both ticket % AND dollar handle % for NBA, MLB, NHL.

Fallback source: Covers.com consensus pages (ticket % only).

Sharp Money Signal
------------------
SBD shows betsPercentage (ticket %) and stakePercentage (dollar handle %).
The divergence between these two signals sharpness:
  • Money > Tickets → sharp/professional money backing the side
  • Low public % + positive CLV → contrarian / sharp setup

Usage
-----
    from scripts.handle_fetcher import fetch_handle_for_game, compute_sharp_score

    bet_pct, money_pct = fetch_handle_for_game(
        game   = "Los Angeles Angels @ Detroit Tigers",
        market = "h2h",
        team   = "Detroit Tigers",
        league = "baseball_mlb",
    )
    # → (69.6, 65.3)  means 69.6% of tickets / 65.3% of money on this side

    score = compute_sharp_score(35.0, 52.0, +130, +120)
    # → 75  (low ticket %, sharp money divergence, line steamed = sharp setup)
"""

import logging
import re
from datetime import date
from typing import Optional, Tuple

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sports Betting Dime JSON API — no auth required
_SBD_URL = "https://www.sportsbettingdime.com/wp-json/adpt/v1/{sport}-odds?format=us"

# Covers.com fallback
_COVERS_URL = "https://contests.covers.com/consensus/topconsensus/{sport}/overall"

_TIMEOUT = 10   # seconds

# Odds-API sport key → SBD / Covers sport slug (same for both)
_SPORT_MAP: dict = {
    "basketball_nba": "nba",
    "baseball_mlb":   "mlb",
    "icehockey_nhl":  "nhl",
}

# Odds-API market key → SBD bettingSplits key
_MARKET_MAP: dict = {
    "h2h":     "moneyline",
    "spreads":  "spread",
    "totals":   "total",
}

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.sportsbettingdime.com/",
    "Origin":          "https://www.sportsbettingdime.com",
})

# In-memory caches: sport_slug → (data, fetched_date_str)
# Both reset when the calendar date changes.
_SBD_CACHE:    dict = {}
_COVERS_CACHE: dict = {}

# ---------------------------------------------------------------------------
# Team name helpers
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Lowercase + normalise whitespace for fuzzy matching."""
    return " ".join(text.lower().split())


def _teams_overlap(a: str, b: str) -> bool:
    """
    True when at least one significant word is shared between two team names.
    Handles short-form names like SBD's "Tigers" matching "Detroit Tigers".
    """
    skip = {"at", "the", "a", "an", "vs", "fc", "sc", "city", "state", "l.a", "la"}
    wa = {w.strip(".,") for w in _slug(a).split() if w.strip(".,") not in skip and len(w.strip(".,")) > 2}
    wb = {w.strip(".,") for w in _slug(b).split() if w.strip(".,") not in skip and len(w.strip(".,")) > 2}
    return bool(wa & wb)


# ---------------------------------------------------------------------------
# Sports Betting Dime — primary source (ticket % + money %)
# ---------------------------------------------------------------------------

def _fetch_sbd_games(sport_key: str) -> list:
    """
    Fetch and cache SBD public betting data for one sport.
    Cache is per calendar day to avoid redundant API calls.
    Returns list of raw game dicts from SBD's JSON response.
    """
    sport = _SPORT_MAP.get(sport_key)
    if not sport:
        return []

    today = date.today().isoformat()
    cached = _SBD_CACHE.get(sport)
    if cached and cached[1] == today:
        return cached[0]

    url = _SBD_URL.format(sport=sport)
    try:
        resp = _SESSION.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        games = resp.json().get("data", [])
    except Exception as exc:
        log.warning("SBD API unavailable (%s): %s", sport, exc)
        _SBD_CACHE[sport] = ([], today)
        return []

    log.debug("SBD: %d games fetched for %s", len(games), sport)
    _SBD_CACHE[sport] = (games, today)
    return games


def _lookup_sbd(
    game: str,
    market: str,
    team: str,
    sport_key: str,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Look up (bet_pct, money_pct) from SBD for one outcome.
    Returns (None, None) when no match or no data available.
    """
    sbd_market = _MARKET_MAP.get(market)
    if not sbd_market:
        return None, None

    games = _fetch_sbd_games(sport_key)
    if not games:
        return None, None

    our_away, our_home = [s.strip() for s in game.split(" @ ", 1)]

    def _match(g: dict) -> bool:
        home = g["competitors"]["home"]
        away = g["competitors"]["away"]
        # Try short name first, then full "market + name" form
        away_match = (
            _teams_overlap(away["name"], our_away)
            or _teams_overlap(away.get("market", "") + " " + away["name"], our_away)
        )
        home_match = (
            _teams_overlap(home["name"], our_home)
            or _teams_overlap(home.get("market", "") + " " + home["name"], our_home)
        )
        return away_match and home_match

    matched = next((g for g in games if _match(g)), None)
    if not matched:
        log.debug("SBD: no match for '%s'", game)
        return None, None

    splits = matched.get("bettingSplits", {})
    market_splits = splits.get(sbd_market)
    if not market_splits:
        return None, None

    if sbd_market == "total":
        # team param will be "Over X.X" or "Under X.X"
        side_key = "over" if "over" in _slug(team) else "under"
    else:
        is_home = _teams_overlap(our_home, team)
        side_key = "home" if is_home else "away"

    side_data = market_splits.get(side_key)
    if not side_data or not isinstance(side_data, dict):
        return None, None

    def _safe_float(v) -> Optional[float]:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    bet_pct   = _safe_float(side_data.get("betsPercentage"))
    money_pct = _safe_float(side_data.get("stakePercentage"))

    log.debug(
        "SBD: %s — %s %s | bets=%.1f%% money=%s%%",
        game, sbd_market, side_key,
        bet_pct or 0,
        f"{money_pct:.1f}" if money_pct is not None else "N/A",
    )
    return bet_pct, money_pct


# ---------------------------------------------------------------------------
# Covers.com — fallback source (ticket % only)
# ---------------------------------------------------------------------------

def _parse_covers_html(html: str) -> list:
    """
    Parse Covers consensus table HTML.
    Returns list of dicts: {away_team, home_team, away_pct, home_pct}.
    """
    games = []
    for row in re.split(r"<tr[^>]*>", html):
        if "covers-CoversConsensus-table--matchupColumn" not in row:
            continue
        away_m = re.search(
            r'covers-CoversConsensus-table--teamBlock["\s][^>]*>.*?title="([^"]+)"',
            row, re.DOTALL
        )
        home_m = re.search(
            r'covers-CoversConsensus-table--teamBlock2["\s][^>]*>.*?title="([^"]+)"',
            row, re.DOTALL
        )
        pcts = re.findall(r"<span>\s*(\d{1,3})%\s*</span>", row)
        if not away_m or not home_m or len(pcts) < 2:
            continue
        try:
            away_pct = float(pcts[0])
            home_pct = float(pcts[1])
        except ValueError:
            continue
        if not (0 < away_pct < 100 and 0 < home_pct < 100):
            continue
        games.append({
            "away_team": away_m.group(1).strip(),
            "home_team": home_m.group(1).strip(),
            "away_pct":  away_pct,
            "home_pct":  home_pct,
        })
    return games


def _fetch_covers_games(sport_key: str) -> list:
    """Fetch and cache Covers consensus data. Returns list of parsed game dicts."""
    sport = _SPORT_MAP.get(sport_key)
    if not sport:
        return []

    today = date.today().isoformat()
    cached = _COVERS_CACHE.get(sport)
    if cached and cached[1] == today:
        return cached[0]

    url = _COVERS_URL.format(sport=sport)
    try:
        resp = _SESSION.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        games = _parse_covers_html(resp.text)
    except Exception as exc:
        log.warning("Covers.com unavailable (%s): %s", sport, exc)
        _COVERS_CACHE[sport] = ([], today)
        return []

    log.debug("Covers.com: %d games parsed for %s", len(games), sport)
    _COVERS_CACHE[sport] = (games, today)
    return games


def _lookup_covers(game: str, team: str, sport_key: str) -> Optional[float]:
    """Look up bet_pct from Covers (ticket % only). Returns None if not found."""
    covers_games = _fetch_covers_games(sport_key)
    if not covers_games:
        return None

    our_away, our_home = [s.strip() for s in game.split(" @ ", 1)]
    matched = next(
        (
            g for g in covers_games
            if _teams_overlap(g["away_team"], our_away)
            and _teams_overlap(g["home_team"], our_home)
        ),
        None,
    )
    if not matched:
        return None

    is_home = _teams_overlap(our_home, team)
    return matched["home_pct"] if is_home else matched["away_pct"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_handle_for_game(
    game:       str,
    market:     str,
    team:       str,
    league:     str,
    game_date:  Optional[str] = None,   # kept for API compatibility; unused
) -> Tuple[Optional[float], Optional[float]]:
    """
    Return (bet_pct, money_pct) for one outcome.

    bet_pct   — % of public bettors who picked this side (0–100), or None
    money_pct — % of dollar handle on this side (0–100), or None

    Primary source: Sports Betting Dime API (both ticket % and money %).
    Fallback source: Covers.com (ticket % only; money_pct = None).

    Parameters
    ----------
    game   : "Away @ Home" string (EVBetCache.game)
    market : "h2h" | "spreads" | "totals"
    team   : outcome label — team name, "Over X.X", "Under X.X"
    league : Odds API sport key, e.g. "basketball_nba"
    """
    if not game or " @ " not in game:
        return None, None

    # Primary: SBD (ticket % + money %)
    bet_pct, money_pct = _lookup_sbd(game, market, team, league)
    if bet_pct is not None:
        return bet_pct, money_pct

    # Fallback: Covers (ticket % only; totals not supported)
    if market != "totals":
        covers_pct = _lookup_covers(game, team, league)
        if covers_pct is not None:
            log.debug("Covers fallback: %s %s = %.0f%% tickets", game, team, covers_pct)
            return covers_pct, None

    return None, None


def compute_sharp_score(
    bet_pct:      Optional[float],
    money_pct:    Optional[float],
    opening_odds: Optional[int],
    current_odds: Optional[int],
) -> Optional[float]:
    """
    Return a 0–100 sharp money score.  Returns None when there is no signal
    at all (no public data AND no line movement).

    Scoring factors
    ---------------
    1a. Public bet% (ticket %) — contrarian signal
        • ≤30%  public on our side → sharps vs. public (+25)
        • 31-40% → mild contrarian  (+12)
        • 41-55% → roughly neutral  (0)
        • 56-65% → mild public lean (-8)
        • >65%   → heavy public lay (-18)
        (used only when money_pct is unavailable)

    1b. Money% vs Bet% divergence (sharp money signal)
        • Each pp of divergence → ±1.6 pts (capped ±40)
        • e.g. money=55%, bets=35% → +32 pts (sharp money flowing in)

    2.  CLV direction (line movement since open)
        • Line shortened toward us (sharp steam in) → +20
        • Line drifted away from us → -10

    Thresholds for the UI badge (dashboard.html):
        ≥65 → ⚡ Sharp Action
    """
    has_public = bet_pct is not None or money_pct is not None
    has_clv    = (
        opening_odds is not None
        and current_odds is not None
        and opening_odds != current_odds
    )

    if not has_public and not has_clv:
        return None

    score = 50.0  # neutral baseline

    # ── Factor 1a: ticket% as contrarian signal (when no money% available) ─
    if bet_pct is not None and money_pct is None:
        if   bet_pct <= 30: score += 25.0
        elif bet_pct <= 40: score += 12.0
        elif bet_pct <= 55: score +=  0.0
        elif bet_pct <= 65: score -=  8.0
        else:               score -= 18.0

    # ── Factor 1b: money% vs bet% divergence ─────────────────────────────
    if bet_pct is not None and money_pct is not None:
        divergence = money_pct - bet_pct
        score += min(40.0, max(-40.0, divergence * 1.6))

    # ── Factor 2: CLV direction ───────────────────────────────────────────
    if has_clv:
        def _to_prob(o: int) -> float:
            return abs(o) / (abs(o) + 100) if o < 0 else 100 / (o + 100)

        open_prob = _to_prob(opening_odds)
        curr_prob = _to_prob(current_odds)

        if curr_prob > open_prob:   # line shortened → sharp steam in
            score += 20.0
        else:                       # line drifted out → money leaving
            score -= 10.0

    return round(max(0.0, min(100.0, score)), 1)
