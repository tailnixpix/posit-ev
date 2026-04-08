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
_MLB_SCHEDULE    = "https://statsapi.mlb.com/api/v1/schedule"    # probable pitchers
_MLB_PEOPLE      = "https://statsapi.mlb.com/api/v1/people"      # pitcher season stats

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
# MLB functions
# ---------------------------------------------------------------------------

def fetch_mlb_probable_pitchers() -> dict:
    """
    Return {game_pk: {"home": {...pitcher info...}, "away": {...pitcher info...}}}
    using the MLB Stats API schedule endpoint with probablePitcher hydration.

    Also fetches season ERA/WHIP/K9 for each pitcher via the people/stats endpoint.
    """
    result: dict = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _get(
        _MLB_SCHEDULE,
        params={
            "sportId": 1,
            "date": today,
            "hydrate": "probablePitcher(note),linescore",
        },
    )

    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            gk = str(game.get("gamePk", ""))
            if not gk:
                continue

            teams = game.get("teams", {})
            entry: dict = {}

            for side in ("home", "away"):
                team_data = teams.get(side, {})
                pp = team_data.get("probablePitcher")
                if not pp:
                    entry[side] = None
                    continue
                pitcher_id = pp.get("id")
                full_name = pp.get("fullName") or f"{pp.get('firstName','')} {pp.get('lastName','')}".strip()
                stats = fetch_mlb_pitcher_stats(pitcher_id) if pitcher_id else {}
                entry[side] = {
                    "id":       pitcher_id,
                    "name":     full_name,
                    "era":      stats.get("era"),
                    "whip":     stats.get("whip"),
                    "k9":       stats.get("strikeoutsPer9Inn"),
                    "wins":     stats.get("wins"),
                    "losses":   stats.get("losses"),
                    "innings":  stats.get("inningsPitched"),
                }

            # Attach team names for easier downstream matching
            entry["home_team"] = teams.get("home", {}).get("team", {}).get("name", "")
            entry["away_team"] = teams.get("away", {}).get("team", {}).get("name", "")
            result[gk] = entry

    log.debug("fetch_mlb_probable_pitchers: %d games found", len(result))
    return result


def fetch_mlb_pitcher_stats(pitcher_id: int) -> dict:
    """
    Return current-season pitching stats for a single pitcher from the MLB Stats API.
    Keys: era, whip, strikeoutsPer9Inn, wins, losses, inningsPitched.
    Returns {} if not found or API error.
    """
    if not pitcher_id:
        return {}
    url = f"{_MLB_PEOPLE}/{pitcher_id}/stats"
    data = _get(url, params={"stats": "season", "group": "pitching"})
    try:
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0].get("stat", {})
        return {
            "era":                 s.get("era"),
            "whip":                s.get("whip"),
            "strikeoutsPer9Inn":   s.get("strikeoutsPer9Inn"),
            "wins":                s.get("wins"),
            "losses":              s.get("losses"),
            "inningsPitched":      s.get("inningsPitched"),
        }
    except Exception:
        return {}


def _match_mlb_game(pitchers: dict, away_team: str, home_team: str) -> Optional[dict]:
    """
    Find the pitcher entry for a given matchup by fuzzy-matching team names.
    Returns the matching entry dict or None.
    """
    for _gk, entry in pitchers.items():
        ht = entry.get("home_team", "")
        at = entry.get("away_team", "")
        # Check for substring match (handles "Milwaukee Brewers" vs "Brewers")
        if (
            (away_team.lower() in at.lower() or at.lower() in away_team.lower()) and
            (home_team.lower() in ht.lower() or ht.lower() in home_team.lower())
        ):
            return entry
    return None


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

        elif sport_key == "baseball_mlb":
            pitchers = fetch_mlb_probable_pitchers()
            # Return a dict keyed by game_pk so ai_analyzer can look up
            # the pitcher entry for each specific game.
            ctx = {
                "_mlb_pitchers": pitchers,  # raw lookup table
                "_sport":        "baseball_mlb",
            }
            log.info("build_context(mlb): %d games with pitcher data", len(pitchers))
            return ctx

    except Exception as exc:
        log.error("build_context(%s) failed: %s", sport_key, exc, exc_info=True)

    return {}


# ---------------------------------------------------------------------------
# MLB local projection model (Pythagorean expectation)
# Used as fallback when Optimal MCP has no MLB data.
# ---------------------------------------------------------------------------

_MLB_LG_ERA     = 4.20   # 2024 MLB league-average ERA (used for normalisation)
_MLB_LG_RPG     = 4.50   # 2024 MLB league-average runs per game per team
_MLB_HOME_BOOST = 1.025  # home-field advantage multiplier (~54% implied win rate)
_MLB_PYTH_EXP   = 1.83   # Pythagorean exponent tuned for baseball

# Within-run caches — reset every process restart (i.e. every pipeline run)
_MLB_TEAM_STATS_CACHE: dict = {}   # team_id (int) → {rpg, era, ops}
_MLB_SCHED_CACHE: dict      = {}   # date_str → list of enriched game entries


def _safe_era(val) -> Optional[float]:
    """Parse a pitcher ERA string/number; return None on failure."""
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def fetch_mlb_team_stats(team_id: int) -> dict:
    """
    Fetch season hitting + pitching stats for one MLB team.

    Returns
    -------
    dict with keys: rpg (runs/game), era (team ERA), ops
    Falls back to league-average constants on any failure.
    """
    if not team_id:
        return {}

    year = datetime.now().year
    base = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats"

    hit_data = _get(base, params={"stats": "season", "group": "hitting",  "season": year})
    pit_data = _get(base, params={"stats": "season", "group": "pitching", "season": year})

    result: dict = {}
    try:
        splits = hit_data.get("stats", [{}])[0].get("splits", [])
        if splits:
            s      = splits[0].get("stat", {})
            games  = int(s.get("gamesPlayed") or 1)
            runs   = float(s.get("runs") or 0)
            result["rpg"] = runs / games if games > 0 else _MLB_LG_RPG
            result["ops"] = float(s.get("ops") or 0)
    except Exception:
        pass

    try:
        splits = pit_data.get("stats", [{}])[0].get("splits", [])
        if splits:
            s = splits[0].get("stat", {})
            result["era"] = float(s.get("era") or _MLB_LG_ERA)
    except Exception:
        pass

    log.debug("fetch_mlb_team_stats(%s): rpg=%.2f era=%.2f",
              team_id, result.get("rpg", _MLB_LG_RPG), result.get("era", _MLB_LG_ERA))
    return result


def _fetch_mlb_team_stats_cached(team_id: int) -> dict:
    """Cached wrapper around fetch_mlb_team_stats — fetches once per pipeline run."""
    if not team_id:
        return {}
    if team_id not in _MLB_TEAM_STATS_CACHE:
        _MLB_TEAM_STATS_CACHE[team_id] = fetch_mlb_team_stats(team_id)
    return _MLB_TEAM_STATS_CACHE[team_id]


def _load_mlb_sched_enriched(date_str: str) -> list:
    """
    Fetch the MLB schedule for one date.  Returns a flat list of game-entry dicts:
      home_team, home_team_id, away_team, away_team_id,
      home / away: {id, name} (pitcher IDs only — ERA fetched lazily in build step)

    Deliberately avoids per-pitcher API calls here; doing 20+ sequential requests
    for a day's worth of starters would time out the projection endpoint.
    """
    data = _get(
        _MLB_SCHEDULE,
        params={
            "sportId": 1,
            "date":    date_str,
            "hydrate": "probablePitcher,team",
        },
    )
    entries = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            teams = game.get("teams", {})
            entry: dict = {}

            for side in ("home", "away"):
                td   = teams.get(side, {})
                team = td.get("team", {})
                pp   = td.get("probablePitcher")

                entry[f"{side}_team"]    = team.get("name", "")
                entry[f"{side}_team_id"] = team.get("id")

                # Store pitcher identity only — ERA fetched separately if needed
                entry[side] = {"id": pp.get("id"), "name": pp.get("fullName", "")} if pp else None

            entries.append(entry)

    log.debug("_load_mlb_sched_enriched(%s): %d games", date_str, len(entries))
    return entries


# Per-pitcher ERA cache (id → era float) — lives for the process lifetime
_MLB_PITCHER_ERA_CACHE: dict = {}


def _get_pitcher_era(pitcher_id: Optional[int]) -> Optional[float]:
    """Fetch and cache ERA for one pitcher. Returns None if unavailable."""
    if not pitcher_id:
        return None
    if pitcher_id not in _MLB_PITCHER_ERA_CACHE:
        stats = fetch_mlb_pitcher_stats(pitcher_id)
        era = _safe_era(stats.get("era"))
        _MLB_PITCHER_ERA_CACHE[pitcher_id] = era   # None is a valid cached result
    return _MLB_PITCHER_ERA_CACHE[pitcher_id]


def _match_mlb_sched_entry(games: list, away_str: str, home_str: str) -> Optional[dict]:
    """
    Fuzzy-match a schedule entry to "Away @ Home" team strings.
    Tries last-word match first, then full substring match as fallback.
    """
    away_kw = away_str.split()[-1].lower()
    home_kw = home_str.split()[-1].lower()

    # Pass 1: last-word match (fast, handles "Yankees" → "New York Yankees")
    for entry in games:
        ht = entry.get("home_team", "").lower()
        at = entry.get("away_team", "").lower()
        if home_kw in ht and away_kw in at:
            return entry

    # Pass 2: full-string substring match (handles multi-word edge cases)
    away_lower = away_str.lower()
    home_lower = home_str.lower()
    for entry in games:
        ht = entry.get("home_team", "").lower()
        at = entry.get("away_team", "").lower()
        if (away_lower in at or at in away_lower) and (home_lower in ht or ht in home_lower):
            return entry

    return None


def build_mlb_game_projection(game: str) -> dict:
    """
    Compute a Pythagorean game projection for an MLB matchup.

    Model
    -----
    away_expected = away_rpg × (home_starter_ERA / LG_ERA)
    home_expected = home_rpg × (away_starter_ERA / LG_ERA) × HOME_BOOST
    total         = away_expected + home_expected
    spread_mean   = home_expected − away_expected   (positive = home favoured)
    home_win_prob via Pythagorean: home^1.83 / (home^1.83 + away^1.83)

    Falls back to league-average ERA when no pitcher is announced.

    Returns same shape as fetch_game_projections() so the rest of the stack
    needs no changes.  Adds source="mlb_pythagorean" to distinguish from Optimal.
    """
    if " @ " not in game:
        return {}

    away_str, home_str = [s.strip() for s in game.split(" @ ", 1)]

    # Find schedule entry — check today and tomorrow so both day/next-day bets work
    entry = None
    for days_ahead in (0, 1):
        date_str = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        if date_str not in _MLB_SCHED_CACHE:
            _MLB_SCHED_CACHE[date_str] = _load_mlb_sched_enriched(date_str)
        entry = _match_mlb_sched_entry(_MLB_SCHED_CACHE[date_str], away_str, home_str)
        if entry:
            break

    if not entry:
        log.debug("build_mlb_game_projection: no schedule match for '%s'", game)
        return {}

    away_team_id = entry.get("away_team_id")
    home_team_id = entry.get("home_team_id")
    away_pitcher = entry.get("away") or {}
    home_pitcher = entry.get("home") or {}

    # Team season stats (2 API calls, cached after first use)
    away_stats = _fetch_mlb_team_stats_cached(away_team_id)
    home_stats = _fetch_mlb_team_stats_cached(home_team_id)

    away_rpg = away_stats.get("rpg", _MLB_LG_RPG)
    home_rpg = home_stats.get("rpg", _MLB_LG_RPG)

    # Starter ERA — lazy-fetched per pitcher (cached), falls back to team ERA
    home_pit_era = (_get_pitcher_era(home_pitcher.get("id"))
                    or home_stats.get("era")
                    or _MLB_LG_ERA)
    away_pit_era = (_get_pitcher_era(away_pitcher.get("id"))
                    or away_stats.get("era")
                    or _MLB_LG_ERA)

    # Expected runs
    away_exp = away_rpg * (home_pit_era / _MLB_LG_ERA)
    home_exp = home_rpg * (away_pit_era / _MLB_LG_ERA) * _MLB_HOME_BOOST

    # Pythagorean win probability
    a = home_exp ** _MLB_PYTH_EXP
    b = away_exp ** _MLB_PYTH_EXP
    home_win_prob = a / (a + b) if (a + b) > 0 else 0.5

    total  = round(away_exp + home_exp, 2)
    spread = round(home_exp - away_exp, 2)   # positive = home favoured

    log.info(
        "build_mlb_game_projection: %s → away=%.2f home=%.2f "
        "total=%.2f spread=%.2f win=%.1f%%",
        game, away_exp, home_exp, total, spread, home_win_prob * 100,
    )

    return {
        "away_team":           away_str,
        "home_team":           home_str,
        "away_display":        away_str,
        "home_display":        home_str,
        "spread_mean":         spread,
        "total_mean":          total,
        "home_score_mean":     round(home_exp, 1),
        "away_score_mean":     round(away_exp, 1),
        "home_win_probability": round(home_win_prob, 4),
        "updated_at":          datetime.now(timezone.utc).isoformat(),
        "source":              "mlb_pythagorean",
    }


# ---------------------------------------------------------------------------
# Game projections — Optimal MCP
# ---------------------------------------------------------------------------

# Odds API league key → Optimal league code
_OPTIMAL_LEAGUE_MAP = {
    "basketball_nba":             "nba",
    "icehockey_nhl":              "nhl",
    "baseball_mlb":               "mlb",
    "soccer_epl":                 "epl",
    "soccer_spain_la_liga":       "laliga",
    "soccer_germany_bundesliga":  "bundesliga",
    "soccer_usa_mls":             "mls",

}


def fetch_game_projections(game: str, league: str) -> dict:
    """
    Fetch Optimal game-level score projections for a specific game.

    Parses the game string ("Away @ Home") and runs a SQL query against the
    Optimal events + game_projections tables to find the matching event and
    return all four projection types (spread, total, homeScore, awayScore)
    plus homeWinProbability.

    Returns a dict with keys:
        away_team, home_team,
        spread_mean, total_mean, home_score_mean, away_score_mean,
        home_win_probability,
        consensus_line, consensus_total,
        event_id, updated_at
    Returns {} on any failure (fault-tolerant).

    Parameters
    ----------
    game : str
        e.g. "St. Louis Blues @ Colorado Avalanche"
    league : str
        Odds API sport key, e.g. "icehockey_nhl"
    """
    if " @ " not in game:
        return {}

    away_str, home_str = [s.strip() for s in game.split(" @ ", 1)]
    opt_league = _OPTIMAL_LEAGUE_MAP.get(league, "")
    if not opt_league:
        log.debug("fetch_game_projections: no Optimal league mapping for %s", league)
        return {}

    try:
        from scripts.optimal_client import _call_tool  # lazy import to avoid circular

        # Use last word of each team name as the fuzzy match key
        # (avoids city vs. nickname mismatches, e.g. "St. Louis" vs "Blues")
        away_kw = away_str.split()[-1].lower()
        home_kw = home_str.split()[-1].lower()

        sql = (
            "SELECT "
            "  e.id AS event_id, "
            "  e.away_display, e.home_display, "
            "  e.consensus_line, e.consensus_total, "
            "  e.consensus_home_ml, e.consensus_away_ml, "
            "  (gp.projections->>'homeWinProbability')::float AS home_win_probability, "
            "  gp.updated_at, "
            "  proj_item->>'projectionType' AS proj_type, "
            "  (proj_item->>'mean')::float AS mean "
            "FROM events e "
            "JOIN game_projections gp ON gp.event_id = e.id, "
            "  LATERAL jsonb_array_elements(gp.projections->'projections') AS proj_item "
            f"WHERE e.league = '{opt_league}' "
            f"  AND e.start_date > NOW() - INTERVAL '4 hours' "
            f"  AND e.start_date < NOW() + INTERVAL '36 hours' "
            f"  AND LOWER(e.away_display) LIKE '%{away_kw}%' "
            f"  AND LOWER(e.home_display) LIKE '%{home_kw}%' "
            "LIMIT 20"
        )

        rows = _call_tool("query", {"sql": sql})
        if not rows or not isinstance(rows, list):
            log.debug("fetch_game_projections: no rows for %s (%s)", game, league)
            rows = []   # fall through to MLB fallback below instead of returning early

        result: dict = {
            "away_team": away_str,
            "home_team": home_str,
        }
        for row in rows:
            # Grab event metadata once
            if "event_id" not in result:
                result["event_id"]          = row.get("event_id")
                result["away_display"]       = row.get("away_display", away_str)
                result["home_display"]       = row.get("home_display", home_str)
                result["consensus_line"]     = row.get("consensus_line")
                result["consensus_total"]    = row.get("consensus_total")
                result["consensus_home_ml"]  = row.get("consensus_home_ml")
                result["consensus_away_ml"]  = row.get("consensus_away_ml")
                result["updated_at"]         = row.get("updated_at")

            if "home_win_probability" not in result and row.get("home_win_probability") is not None:
                result["home_win_probability"] = row["home_win_probability"]

            # Pivot projection rows
            pt   = row.get("proj_type", "")
            mean = row.get("mean")
            if pt == "spread":
                result["spread_mean"]     = mean
            elif pt == "total":
                result["total_mean"]      = mean
            elif pt == "homeScore":
                result["home_score_mean"] = mean
            elif pt == "awayScore":
                result["away_score_mean"] = mean

        if "spread_mean" not in result and "total_mean" not in result:
            log.debug("fetch_game_projections: matched rows but no projection data for %s", game)
            # Fall through to MLB fallback below
        else:
            log.info(
                "fetch_game_projections: %s spread=%.2f total=%.2f home_win=%.1f%%",
                game,
                result.get("spread_mean", 0),
                result.get("total_mean", 0),
                (result.get("home_win_probability") or 0) * 100,
            )
            return result

    except Exception as exc:
        log.warning("fetch_game_projections failed for %s: %s", game, exc)
        # Fall through to MLB fallback below

    # MLB fallback — Pythagorean model from team stats + pitcher ERA
    if league == "baseball_mlb":
        log.debug("fetch_game_projections: trying MLB Pythagorean fallback for %s", game)
        return build_mlb_game_projection(game)

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

    print("\n=== MLB pitcher context ===")
    mlb = build_context("baseball_mlb")
    pitchers = mlb.get("_mlb_pitchers", {})
    if pitchers:
        for gk, entry in list(pitchers.items())[:3]:
            away = entry.get("away") or {}
            home = entry.get("home") or {}
            print(f"  {entry.get('away_team')} @ {entry.get('home_team')}")
            print(f"    Away SP: {away.get('name')} ERA={away.get('era')} WHIP={away.get('whip')}")
            print(f"    Home SP: {home.get('name')} ERA={home.get('era')} WHIP={home.get('whip')}")
    else:
        print("  (empty — no games today or API unavailable)")
