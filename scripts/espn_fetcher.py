"""
espn_fetcher.py — Injury reports from ESPN's unofficial public JSON API.

The endpoint at site.api.espn.com/apis/site/v2/sports/{sport}/{league}/injuries
is widely used, unauthenticated, and stable across NBA, MLB, NHL, and soccer.

Usage
-----
    # Once per pipeline run — pre-fetch per sport to avoid N+1 HTTP calls:
    cache = fetch_injuries_for_sport("basketball_nba")

    # Per game:
    alert = get_injury_alert("Celtics", "Heat", "basketball_nba", injury_cache=cache)
    # → "Jaylen Brown (SG) Out — Knee [Boston Celtics]"
"""

import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_TIMEOUT   = 8   # seconds

# Statuses that materially affect lines / game totals
_KEY_STATUSES = {"out", "doubtful"}

# ESPN sport path per Odds API sport key
_SPORT_PATH: dict[str, str] = {
    "basketball_nba":            "basketball/nba",
    "baseball_mlb":              "baseball/mlb",
    "icehockey_nhl":             "hockey/nhl",
    "soccer_epl":                "soccer/eng.1",
    "soccer_spain_la_liga":      "soccer/esp.1",
    "soccer_germany_bundesliga": "soccer/ger.1",
    "soccer_usa_mls":            "soccer/usa.1",
    "soccer_uefa_champs_league": "soccer/uefa.champions",
}


def fetch_injuries_for_sport(sport_key: str) -> list[dict]:
    """
    Return a flat list of injury records for the given sport key.

    Each record: {team, team_abbr, player, position, status, detail}
    Returns [] on any error (non-fatal — pipeline continues without it).
    """
    path = _SPORT_PATH.get(sport_key)
    if not path:
        return []

    url = f"{_ESPN_BASE}/{path}/injuries"
    try:
        resp = requests.get(
            url, timeout=_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PositEV/1.0)"},
        )
        if resp.status_code != 200:
            log.debug("ESPN injuries %s: HTTP %d", path, resp.status_code)
            return []

        data = resp.json()
        # Shape: {"injuries": [{"team": {...}, "injuries": [{athlete, type, status}]}]}
        records = []
        for team_block in data.get("injuries", []):
            team_info = team_block.get("team", {})
            team_name = team_info.get("displayName", "")
            team_abbr = team_info.get("abbreviation", "")
            for inj in team_block.get("injuries", []):
                athlete = inj.get("athlete", {})
                records.append({
                    "team":      team_name,
                    "team_abbr": team_abbr,
                    "player":    athlete.get("displayName", ""),
                    "position":  athlete.get("position", {}).get("abbreviation", ""),
                    "status":    inj.get("status", ""),
                    "detail":    inj.get("type", {}).get("text", ""),
                })
        log.info("ESPN injuries %s: fetched %d records", sport_key, len(records))
        return records

    except Exception as exc:
        log.debug("ESPN injuries %s: %s", sport_key, exc)
        return []


def get_injury_alert(
    home_team: str,
    away_team: str,
    sport_key: str,
    injury_cache: Optional[list[dict]] = None,
) -> Optional[str]:
    """
    Return a short injury-alert string for a game, or None if no key absences.

    Matches injuries to the game via fuzzy word overlap on team names
    (e.g. "Boston Celtics" matches "Celtics").

    Pass injury_cache (pre-fetched from fetch_injuries_for_sport) to avoid
    one HTTP call per game; if None, fetches fresh.
    """
    if injury_cache is None:
        injury_cache = fetch_injuries_for_sport(sport_key)

    if not injury_cache:
        return None

    def _matches(inj_team: str, game_team: str) -> bool:
        """True if any word in inj_team overlaps with game_team."""
        return bool(
            set(inj_team.lower().split()) & set(game_team.lower().split())
        )

    alerts = []
    for rec in injury_cache:
        if rec.get("status", "").lower() not in _KEY_STATUSES:
            continue
        team = rec.get("team", "")
        if not (_matches(team, home_team) or _matches(team, away_team)):
            continue

        player  = rec.get("player", "?")
        pos     = rec.get("position", "")
        status  = rec.get("status", "Out")
        detail  = rec.get("detail", "")
        pos_str = f" ({pos})" if pos else ""
        det_str = f" — {detail}" if detail else ""
        alerts.append(f"{player}{pos_str} {status}{det_str}")

    if not alerts:
        return None

    return " · ".join(alerts[:3])   # cap at 3 injuries per game
