"""
context_fetcher.py — Fetch free NHL/NBA context data to power sport adjustments.

All functions are fault-tolerant: any network or parse error returns an empty
dict silently so the pipeline never breaks due to a bad API response.

Free APIs used (no key required):
  - NHL schedule / goalies : api-web.nhle.com
  - NHL/NBA records, B2B   : site.api.espn.com (ESPN public API)
"""

import logging
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from typing import Optional

import requests

log = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "positiv-ev/1.0"})

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_NHL_SCHEDULE    = "https://api-web.nhle.com/v1/schedule/now"
_NHL_STANDINGS   = "https://api-web.nhle.com/v1/standings/now"   # has homeWins/roadWins per team
_ESPN_NHL_TEAMS  = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/teams"   # for injuries
_ESPN_NBA_TEAMS  = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"  # for injuries
_ESPN_NBA_STAND  = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"   # home/road splits
_ESPN_NBA_BOARD  = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"  # B2B

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict = None, timeout: int = 8) -> dict:
    """GET with single retry. Returns parsed JSON dict or {} on failure."""
    try:
        r = _SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.debug("context_fetcher _get failed %s: %s", url, exc)
        return {}


def _normalise(name: str) -> str:
    """Lower-case, strip punctuation — for fuzzy matching."""
    return name.lower().replace("-", " ").replace(".", "").replace("'", "").strip()


def match_team(query: str, candidates: list) -> Optional[str]:
    """
    Fuzzy-match a team name against a list of known names.
    Returns the best match if ratio >= 0.60, else None.
    """
    if not query or not candidates:
        return None
    q = _normalise(query)
    best, best_ratio = None, 0.0
    for c in candidates:
        ratio = SequenceMatcher(None, q, _normalise(c)).ratio()
        if ratio > best_ratio:
            best, best_ratio = c, ratio
    return best if best_ratio >= 0.60 else None


def _win_pct(wins: int, losses: int) -> float:
    total = wins + losses
    return wins / total if total > 0 else 0.5


# ---------------------------------------------------------------------------
# NHL functions
# ---------------------------------------------------------------------------

def fetch_nhl_goalies() -> dict:
    """
    Return {team_name: {"confirmed": bool, "starter": str|None}} for today's
    NHL games using the NHL public API schedule endpoint.

    Goalie data is surfaced when a "startingGoalie" key appears under each
    side in the schedule payload.  If absent (common 24h+ before puck drop),
    confirmed=False is returned.
    """
    result: dict = {}
    data = _get(_NHL_SCHEDULE)

    for week in data.get("gameWeek", []):
        for game in week.get("games", []):
            for side in ("homeTeam", "awayTeam"):
                team_info = game.get(side, {})
                # Team name: try full name, then city, then abbreviation
                name = (
                    team_info.get("placeName", {}).get("default")
                    or team_info.get("commonName", {}).get("default")
                    or team_info.get("abbrev", "")
                )
                # The NHL API surfaces the starting goalie in the schedule
                # only once confirmed (typically ~90 min before puck drop).
                starter = team_info.get("startingGoalie")
                if starter:
                    goalie_name = (
                        starter.get("name", {}).get("default")
                        or f"{starter.get('firstName', {}).get('default', '')} "
                           f"{starter.get('lastName', {}).get('default', '')}".strip()
                    )
                    result[name] = {"confirmed": True, "starter": goalie_name or None}
                else:
                    # Not yet confirmed — could also just be too early
                    result[name] = {"confirmed": False, "starter": None}

    log.debug("fetch_nhl_goalies: %d teams found", len(result))
    return result


def fetch_nhl_home_away_splits() -> dict:
    """
    Return {team_name: {"home_win_pct": float, "away_win_pct": float}}
    using the NHL public API standings endpoint (api-web.nhle.com).

    Calculates win% as wins / (wins + losses + otLosses).
    """
    result: dict = {}
    data = _get(_NHL_STANDINGS)

    for entry in data.get("standings", []):
        name = (
            entry.get("teamName", {}).get("default")
            or entry.get("teamCommonName", {}).get("default")
            or entry.get("teamAbbrev", {}).get("default", "")
        )
        if not name:
            continue

        hw = int(entry.get("homeWins", 0) or 0)
        hl = int(entry.get("homeLosses", 0) or 0)
        ho = int(entry.get("homeOtLosses", 0) or 0)
        rw = int(entry.get("roadWins", 0) or 0)
        rl = int(entry.get("roadLosses", 0) or 0)
        ro = int(entry.get("roadOtLosses", 0) or 0)

        home_total = hw + hl + ho
        road_total = rw + rl + ro
        result[name] = {
            "home_win_pct": hw / home_total if home_total else 0.5,
            "away_win_pct": rw / road_total if road_total else 0.5,
        }

    log.debug("fetch_nhl_home_away_splits: %d teams found", len(result))
    return result


def fetch_nhl_injuries() -> dict:
    """
    Return {team_name: [injured_player_names]}.
    ESPN teams endpoint may include injuries under team.injuries.
    Returns {} if the field is absent (no error raised).
    """
    result: dict = {}
    data = _get(_ESPN_NHL_TEAMS)

    try:
        teams = (
            data.get("sports", [{}])[0]
                .get("leagues", [{}])[0]
                .get("teams", [])
        )
    except (IndexError, AttributeError):
        teams = []

    for entry in teams:
        t = entry.get("team", {})
        name = t.get("displayName") or ""
        if not name:
            continue
        injuries = [
            inj.get("athlete", {}).get("displayName", "")
            for inj in t.get("injuries", [])
            if inj.get("athlete", {}).get("displayName")
        ]
        result[name] = injuries

    log.debug("fetch_nhl_injuries: %d teams found", len(result))
    return result


# ---------------------------------------------------------------------------
# NBA functions
# ---------------------------------------------------------------------------

def fetch_nba_b2b() -> dict:
    """
    Return {team_name: True} for NBA teams that played yesterday and are
    therefore on a back-to-back today.

    Checks the ESPN NBA scoreboard for yesterday's games.
    """
    b2b_teams: set = set()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    data = _get(_ESPN_NBA_BOARD, params={"dates": yesterday})

    for event in data.get("events", []):
        for competition in event.get("competitions", []):
            for competitor in competition.get("competitors", []):
                name = competitor.get("team", {}).get("displayName", "")
                if name:
                    b2b_teams.add(name)

    result = {name: True for name in b2b_teams}
    log.debug("fetch_nba_b2b: %d teams on B2B", len(result))
    return result


def _parse_record_str(rec_str: str) -> tuple:
    """Parse a 'W-L' record string like '26-11'. Returns (wins, losses)."""
    try:
        parts = str(rec_str).split("-")
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


def fetch_nba_home_away_splits() -> dict:
    """
    Return {team_name: {"home_win_pct": float, "away_win_pct": float}}
    using the ESPN NBA standings endpoint.

    The standings API returns Home/Road records as displayValue strings ("26-11").
    """
    result: dict = {}
    data = _get(_ESPN_NBA_STAND)

    try:
        all_entries = []
        for child in data.get("children", []):
            all_entries.extend(child.get("standings", {}).get("entries", []))
    except Exception:
        all_entries = []

    for entry in all_entries:
        name = entry.get("team", {}).get("displayName", "")
        if not name:
            continue
        stats = entry.get("stats", [])
        home_rec = next((s for s in stats if s.get("name") == "Home"), None)
        road_rec = next((s for s in stats if s.get("name") == "Road"), None)
        hw, hl = _parse_record_str(home_rec.get("displayValue", "0-0")) if home_rec else (0, 0)
        rw, rl = _parse_record_str(road_rec.get("displayValue", "0-0")) if road_rec else (0, 0)
        result[name] = {
            "home_win_pct": _win_pct(hw, hl),
            "away_win_pct": _win_pct(rw, rl),
        }

    log.debug("fetch_nba_home_away_splits: %d teams found", len(result))
    return result


def fetch_nba_injuries() -> dict:
    """
    Return {team_name: [injured_player_names]} from ESPN NBA teams endpoint.
    """
    result: dict = {}
    data = _get(_ESPN_NBA_TEAMS)

    try:
        teams = (
            data.get("sports", [{}])[0]
                .get("leagues", [{}])[0]
                .get("teams", [])
        )
    except (IndexError, AttributeError):
        teams = []

    for entry in teams:
        t = entry.get("team", {})
        name = t.get("displayName") or ""
        if not name:
            continue
        injuries = [
            inj.get("athlete", {}).get("displayName", "")
            for inj in t.get("injuries", [])
            if inj.get("athlete", {}).get("displayName")
        ]
        result[name] = injuries

    log.debug("fetch_nba_injuries: %d teams found", len(result))
    return result


# ---------------------------------------------------------------------------
# Assembler — single entry point used by report_generator
# ---------------------------------------------------------------------------

def build_context(sport_key: str) -> dict:
    """
    Fetch all context for a sport and return a mapping:

        {normalised_team_name: {
            "home_win_pct":     float,   # win% at home this season
            "away_win_pct":     float,   # win% on road this season
            "goalie_confirmed": bool,    # NHL only
            "goalie_name":      str|None,
            "injuries":         list[str],
            "b2b":              bool,    # NBA only
        }}

    Returns {} on any failure so the pipeline degrades gracefully.
    All values default to neutral (0.5 win%, no injuries, B2B=False).
    """
    try:
        if sport_key == "icehockey_nhl":
            goalies  = fetch_nhl_goalies()
            splits   = fetch_nhl_home_away_splits()
            injuries = fetch_nhl_injuries()

            all_names = set(goalies) | set(splits) | set(injuries)
            ctx: dict = {}
            for name in all_names:
                ctx[_normalise(name)] = {
                    "home_win_pct":     splits.get(name, {}).get("home_win_pct", 0.5),
                    "away_win_pct":     splits.get(name, {}).get("away_win_pct", 0.5),
                    "goalie_confirmed": goalies.get(name, {}).get("confirmed", None),
                    "goalie_name":      goalies.get(name, {}).get("starter"),
                    "injuries":         injuries.get(name, []),
                    "b2b":              False,
                }
            log.info("build_context(nhl): %d teams populated", len(ctx))
            return ctx

        elif sport_key == "basketball_nba":
            b2b      = fetch_nba_b2b()
            splits   = fetch_nba_home_away_splits()
            injuries = fetch_nba_injuries()

            all_names = set(b2b) | set(splits) | set(injuries)
            ctx = {}
            for name in all_names:
                ctx[_normalise(name)] = {
                    "home_win_pct":     splits.get(name, {}).get("home_win_pct", 0.5),
                    "away_win_pct":     splits.get(name, {}).get("away_win_pct", 0.5),
                    "goalie_confirmed": None,
                    "goalie_name":      None,
                    "injuries":         injuries.get(name, []),
                    "b2b":              bool(b2b.get(name, False)),
                }
            log.info("build_context(nba): %d teams populated", len(ctx))
            return ctx

    except Exception as exc:
        log.error("build_context(%s) failed: %s", sport_key, exc, exc_info=True)

    return {}


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    print("\n=== NHL context ===")
    nhl = build_context("icehockey_nhl")
    if nhl:
        sample = list(nhl.items())[:3]
        for name, data in sample:
            print(f"  {name}: {data}")
    else:
        print("  (empty — no games today or API unavailable)")

    print("\n=== NBA context ===")
    nba = build_context("basketball_nba")
    if nba:
        sample = list(nba.items())[:3]
        for name, data in sample:
            print(f"  {name}: {data}")
    else:
        print("  (empty — no games today or API unavailable)")
