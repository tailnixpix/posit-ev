"""
scripts/handle_fetcher.py — Public betting consensus & sharp money signal.

Source: Covers.com consensus pages (HTML scrape — no auth required).
Provides the % of public bettors on each side for NBA, MLB, NHL.

Sharp Money Signal
------------------
Covers shows what percentage of their users picked each side of a spread.
This is a reliable "public sentiment" proxy:

  • Low public % on a side  + our model finds +EV  → contrarian / sharp setup
  • Positive CLV (line moved in our direction since open) → professional steam

Combined, these produce the sharp_score stored on each card.

Usage
-----
    from scripts.handle_fetcher import fetch_handle_for_game, compute_sharp_score

    bet_pct, money_pct = fetch_handle_for_game(
        game   = "Miami Heat @ Boston Celtics",
        market = "h2h",
        team   = "Boston Celtics",
        league = "basketball_nba",
    )
    # → (35.0, None)  means 35% of public picked this side (money% not available)

    score = compute_sharp_score(35.0, None, +130, +120)
    # → 70  (low public support + line steamed = sharp setup)
"""

import logging
import re
from datetime import date, datetime, timezone
from typing import Optional, Tuple

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://contests.covers.com/consensus/topconsensus/{sport}/overall"
_TIMEOUT  = 10   # seconds

# Odds-API sport key → Covers sport slug
_SPORT_MAP: dict = {
    "basketball_nba": "nba",
    "baseball_mlb":   "mlb",
    "icehockey_nhl":  "nhl",
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
    "Accept":          "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.covers.com/",
})

# In-memory cache: sport_slug → (parsed_games, fetched_date_str)
# Invalidated when the calendar date changes.
_GAME_CACHE: dict = {}

# ---------------------------------------------------------------------------
# HTML parser helpers
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Lowercase + normalise whitespace for fuzzy matching."""
    return " ".join(text.lower().split())


def _teams_overlap(a: str, b: str) -> bool:
    """
    True when at least one significant word is shared between two team names.
    Handles short-form names Covers uses ("Hou" → "Houston Rockets"):
      e.g. "L.A. Lakers" matches "Los Angeles Lakers" via "Lakers".
    """
    skip = {"at", "the", "a", "an", "vs", "fc", "sc", "city", "state", "l.a", "la"}
    wa = {w.strip(".,") for w in _slug(a).split() if w.strip(".,") not in skip and len(w.strip(".,")) > 2}
    wb = {w.strip(".,") for w in _slug(b).split() if w.strip(".,") not in skip and len(w.strip(".,")) > 2}
    return bool(wa & wb)


def _parse_covers_html(html: str) -> list:
    """
    Parse the Covers consensus table HTML.

    Returns list of dicts:
        {
          "away_team": "Houston",
          "home_team": "L.A. Lakers",
          "away_pct":  27.0,   # % of public on away side
          "home_pct":  73.0,
        }

    The Covers page is server-rendered: each game is one <tr> with:
      • Team names in title= attributes inside .--teamBlock / .--teamBlock2
      • Bet percentages in the two <span>NN%</span> inside the consensus <td>
        (first = away, second = home — consistent regardless of --high/--low class)
    """
    games = []

    # Split on <tr> boundaries; skip header row
    rows = re.split(r"<tr[^>]*>", html)

    for row in rows:
        if "covers-CoversConsensus-table--matchupColumn" not in row:
            continue

        # Away team — title attribute inside first teamBlock span
        away_m = re.search(
            r'covers-CoversConsensus-table--teamBlock["\s][^>]*>.*?title="([^"]+)"',
            row, re.DOTALL
        )
        # Home team — title attribute inside teamBlock2 span
        home_m = re.search(
            r'covers-CoversConsensus-table--teamBlock2["\s][^>]*>.*?title="([^"]+)"',
            row, re.DOTALL
        )
        # All percentage values in this row (first two belong to the consensus column)
        pcts = re.findall(r"<span>\s*(\d{1,3})%\s*</span>", row)

        if not away_m or not home_m or len(pcts) < 2:
            continue

        away_team = away_m.group(1).strip()
        home_team = home_m.group(1).strip()
        try:
            away_pct = float(pcts[0])
            home_pct = float(pcts[1])
        except ValueError:
            continue

        # Sanity: percentages should be roughly complementary
        if not (0 < away_pct < 100 and 0 < home_pct < 100):
            continue

        games.append({
            "away_team": away_team,
            "home_team": home_team,
            "away_pct":  away_pct,
            "home_pct":  home_pct,
        })

    return games


# ---------------------------------------------------------------------------
# Data fetcher
# ---------------------------------------------------------------------------

def _fetch_covers_games(sport_key: str) -> list:
    """
    Fetch and cache Covers consensus data for one sport.
    Cache is per calendar day (ET) to avoid redundant scrapes within a pipeline run.
    Returns list of parsed game dicts (see _parse_covers_html).
    """
    covers_sport = _SPORT_MAP.get(sport_key)
    if not covers_sport:
        return []

    today = date.today().isoformat()
    cached = _GAME_CACHE.get(covers_sport)
    if cached and cached[1] == today:
        return cached[0]

    url = _BASE_URL.format(sport=covers_sport)
    try:
        resp = _SESSION.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        log.warning("Covers.com unavailable (%s): %s", covers_sport, exc)
        _GAME_CACHE[covers_sport] = ([], today)
        return []

    games = _parse_covers_html(html)
    log.debug("Covers.com: %d games parsed for %s", len(games), covers_sport)
    _GAME_CACHE[covers_sport] = (games, today)
    return games


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

    bet_pct   — % of Covers users who picked this side (0–100), or None
    money_pct — always None (Covers does not publish money/handle %)

    Parameters
    ----------
    game   : "Away @ Home" string (EVBetCache.game)
    market : "h2h" | "spreads" | "totals"
    team   : outcome label — team name, "Over X.X", "Under X.X"
    league : Odds API sport key, e.g. "basketball_nba"
    """
    # Covers only covers spread sides, not totals props
    if market == "totals":
        return None, None
    if not game or " @ " not in game:
        return None, None

    covers_games = _fetch_covers_games(league)
    if not covers_games:
        return None, None

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
        log.debug("Covers: no match for '%s'", game)
        return None, None

    # Determine which side our bet is on
    is_home = _teams_overlap(our_home, team)
    bet_pct = matched["home_pct"] if is_home else matched["away_pct"]

    log.debug(
        "Covers: %s — %s side = %.0f%% public (away=%.0f%% home=%.0f%%)",
        game, "home" if is_home else "away",
        bet_pct, matched["away_pct"], matched["home_pct"],
    )
    return bet_pct, None   # money_pct not available from Covers


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
    1. Public bet% (from Covers) — contrarian signal
       • ≤30%  public on our side → sharps vs. public (+25)
       • 31-40% → mild contrarian  (+12)
       • 41-55% → roughly neutral  (0)
       • 56-65% → mild public lean (-8)
       • >65%   → heavy public lay (-18)

    2. CLV direction (line movement since open)
       • Line shortened toward us (sharp steam in) → +20
       • Line drifted away from us → -10

    3. Money% vs Bet% divergence (future-proof; applies if money_pct ever
       becomes available again from another source)
       • Each pp of divergence → ±1.6 pts (capped ±40)

    Thresholds for the UI badge (dashboard.html):
        ≥65 → ⚡ Sharp Action
    """
    has_public = bet_pct is not None or money_pct is not None
    has_clv    = (
        opening_odds is not None
        and current_odds is not None
        and opening_odds != current_odds
    )

    # Nothing to work with — no badge
    if not has_public and not has_clv:
        return None

    score = 50.0  # neutral baseline

    # ── Factor 1: Public bet% as contrarian signal ────────────────────────
    if bet_pct is not None and money_pct is None:
        if   bet_pct <= 30: score += 25.0
        elif bet_pct <= 40: score += 12.0
        elif bet_pct <= 55: score +=  0.0   # neutral
        elif bet_pct <= 65: score -=  8.0
        else:               score -= 18.0

    # ── Factor 1b: Money% vs Bet% divergence (if money_pct available) ────
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
