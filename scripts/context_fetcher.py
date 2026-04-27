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


def fetch_nhl_team_form() -> dict:
    """
    Return {team_name: {"last10_wins": int, "streak_val": int}}
    using the NHL standings endpoint (same data as fetch_nhl_home_away_splits,
    no additional API call — endpoint is cached in-process).

    last10_wins: wins in last 10 games (0–10; 5 = league average)
    streak_val:  +N = current N-game win streak, -N = N-game loss/OTL streak
    """
    result: dict = {}
    data = _get(_NHL_STANDINGS)
    if not data:
        return result

    for entry in data.get("standings", []):
        name = (
            entry.get("teamName", {}).get("default")
            or entry.get("teamCommonName", {}).get("default")
            or entry.get("teamAbbrev", {}).get("default", "")
        )
        if not name:
            continue

        l10w = int(entry.get("l10Wins", 0) or 0)

        # NHL standings don't include current streak directly; approximate from
        # recent wins/losses. The streak_val field is left at 0 here — it will
        # be enriched by fetch_game_context() at the per-game AI analysis level.
        result[name] = {"last10_wins": l10w, "streak_val": 0}

    log.debug("fetch_nhl_team_form: %d teams", len(result))
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


def fetch_nba_team_form() -> dict:
    """
    Return {team_name: {"last10_wins": int, "streak_val": int}}
    using the ESPN NBA standings endpoint.

    last10_wins: wins in last 10 games (0–10; 5 = league average)
    streak_val:  +N = current N-game win streak, -N = N-game loss streak
                 (positive = winning, negative = losing)
    """
    result: dict = {}
    data = _get(_ESPN_NBA_STAND)
    if not data:
        return result

    try:
        all_entries = []
        for child in data.get("children", []):
            all_entries.extend(child.get("standings", {}).get("entries", []))
    except Exception:
        return result

    for entry in all_entries:
        name = entry.get("team", {}).get("displayName", "")
        if not name:
            continue
        stats = {s.get("name"): s for s in entry.get("stats", [])}

        # Last 10 games
        l10_stat = stats.get("Last Ten Games") or stats.get("L10") or {}
        l10_str  = l10_stat.get("displayValue", "")   # e.g. "7-3"
        try:
            l10w = int(l10_str.split("-")[0])
        except Exception:
            l10w = 5  # neutral default

        # Current streak from streak stat if available
        streak_stat = stats.get("streak") or stats.get("Streak") or {}
        streak_str  = streak_stat.get("displayValue", "")   # e.g. "W3" or "L2"
        streak_val  = 0
        try:
            if streak_str.startswith("W"):
                streak_val = int(streak_str[1:])
            elif streak_str.startswith("L"):
                streak_val = -int(streak_str[1:])
        except Exception:
            pass

        result[name] = {"last10_wins": l10w, "streak_val": streak_val}

    log.debug("fetch_nba_team_form: %d teams", len(result))
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


def fetch_pitcher_vs_team_stats(pitcher_id: int, opposing_team_id: int) -> dict:
    """
    Fetch a pitcher's historical stats against a specific opposing team.

    Uses the MLB Stats API vsTeam endpoint. Returns an empty dict on any failure.
    Data includes career and current-season stats vs. this opponent:
    games, innings pitched, ERA, WHIP, strikeouts, walks, hits allowed.
    """
    if not pitcher_id or not opposing_team_id:
        return {}
    year = datetime.now().year
    try:
        url = f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
        data = _get(url, params={
            "stats": "vsTeam",
            "group": "pitching",
            "opposingTeamId": opposing_team_id,
            "season": year,
        })
        if not data:
            return {}
        splits = data.get("stats", [{}])[0].get("splits", [])
        if not splits:
            return {}
        s = splits[0].get("stat", {})
        result = {
            "games":    s.get("gamesStarted", s.get("gamesPitched", 0)),
            "ip":       s.get("inningsPitched", "0.0"),
            "era":      s.get("era", "-.--"),
            "whip":     s.get("whip", "-.--"),
            "strikeouts": s.get("strikeOuts", 0),
            "walks":    s.get("baseOnBalls", 0),
            "hits":     s.get("hits", 0),
            "runs":     s.get("earnedRuns", 0),
        }
        log.debug("fetch_pitcher_vs_team_stats: pitcher %s vs team %s → %s", pitcher_id, opposing_team_id, result)
        return result
    except Exception as exc:
        log.debug("fetch_pitcher_vs_team_stats: failed pitcher=%s team=%s: %s", pitcher_id, opposing_team_id, exc)
        return {}


# ---------------------------------------------------------------------------
# MLB functions
# ---------------------------------------------------------------------------

def fetch_mlb_probable_pitchers() -> dict:
    """
    Return {game_pk: {"home": {...pitcher info...}, "away": {...pitcher info...}}}
    using the MLB Stats API schedule endpoint with probablePitcher hydration.

    Fetches both today AND tomorrow so the AI analysis has pitcher data for
    next-day games (starters are posted 24+ hours in advance).

    Also fetches season ERA/WHIP/K9 for each pitcher via the people/stats endpoint.
    """
    result: dict = {}
    now_utc = datetime.now(timezone.utc)

    # Build date range: today through tomorrow (start_date=today&end_date=tomorrow
    # fetches both in one request, reducing latency and API calls)
    start_date = now_utc.strftime("%Y-%m-%d")
    end_date   = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")

    data = _get(
        _MLB_SCHEDULE,
        params={
            "sportId":    1,
            "startDate":  start_date,
            "endDate":    end_date,
            "hydrate":    "probablePitcher(note),linescore",
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
            # Tag which calendar date this game falls on (useful for debugging)
            entry["game_date"] = date_entry.get("date", "")
            result[gk] = entry

    log.debug(
        "fetch_mlb_probable_pitchers: %d games found across %s→%s",
        len(result), start_date, end_date,
    )
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
# Prop-context helpers — platoon splits (MLB) + goalie / SOG stats (NHL)
# ---------------------------------------------------------------------------

_MLB_PEOPLE_SEARCH = "https://statsapi.mlb.com/api/v1/people/search"
_NHL_PLAYER_SEARCH = "https://search.d3.nhle.com/api/v1/search"
_NHL_PLAYER_LAND   = "https://api-web.nhle.com/v1/player"   # /{id}/landing


def _fetch_mlb_player_id(player_name: str) -> Optional[int]:
    """
    Look up a current MLB player's ID by name via the MLB Stats API.
    Returns None if not found or on error.
    """
    if not player_name:
        return None
    data = _get(_MLB_PEOPLE_SEARCH, params={
        "names":   player_name,
        "sportId": 1,       # MLB
        "active":  "true",
    })
    people = data.get("people", [])
    if people:
        return people[0].get("id")
    return None


def fetch_mlb_pitcher_platoon_splits(pitcher_id: int) -> dict:
    """
    Return this season's pitching splits vs LHB and vs RHB for a pitcher.

    Keys in each sub-dict: ba (avg), era, whip, k_pct, bb_pct, hr, pa, ab.

    Uses MLB Stats API statSplits endpoint with sitCodes vl (vs left) and
    vr (vs right).  Returns {} on any failure.
    """
    if not pitcher_id:
        return {}
    year = datetime.now().year
    data = _get(
        f"{_MLB_PEOPLE}/{pitcher_id}/stats",
        params={
            "stats":    "statSplits",
            "group":    "pitching",
            "season":   year,
            "sitCodes": "vl,vr",
        },
    )
    result: dict = {}
    try:
        for stat_group in data.get("stats", []):
            for split in stat_group.get("splits", []):
                code = split.get("split", {}).get("code", "")
                s    = split.get("stat", {})
                pa   = int(s.get("plateAppearances", 0) or 0)
                k    = int(s.get("strikeOuts", 0) or 0)
                bb   = int(s.get("baseOnBalls", 0) or 0)
                entry = {
                    "pa":    pa,
                    "ab":    int(s.get("atBats", 0) or 0),
                    "ba":    s.get("avg", "---"),
                    "era":   s.get("era", "---"),
                    "whip":  s.get("whip", "---"),
                    "k_pct": f"{k/pa*100:.1f}%" if pa > 0 else "---",
                    "bb_pct":f"{bb/pa*100:.1f}%" if pa > 0 else "---",
                    "hr":    int(s.get("homeRuns", 0) or 0),
                    "obp":   s.get("obp", "---"),
                    "slg":   s.get("slg", "---"),
                }
                if code == "vl":
                    result["vs_lhb"] = entry
                elif code == "vr":
                    result["vs_rhb"] = entry
    except Exception as exc:
        log.debug("fetch_mlb_pitcher_platoon_splits failed for id=%s: %s", pitcher_id, exc)
    return result


def fetch_mlb_batter_platoon_splits(player_name: str) -> dict:
    """
    Return this season's hitting splits vs LHP and vs RHP for a batter.
    Looks up the player ID by name first.

    Keys in each sub-dict: ba, obp, slg, ops, hr, k_pct, bb_pct, pa, ab.

    Returns {} on any failure.
    """
    player_id = _fetch_mlb_player_id(player_name)
    if not player_id:
        log.debug("fetch_mlb_batter_platoon_splits: player not found: %s", player_name)
        return {}
    year = datetime.now().year
    data = _get(
        f"{_MLB_PEOPLE}/{player_id}/stats",
        params={
            "stats":    "statSplits",
            "group":    "hitting",
            "season":   year,
            "sitCodes": "vl,vr",
        },
    )
    result: dict = {}
    try:
        for stat_group in data.get("stats", []):
            for split in stat_group.get("splits", []):
                code = split.get("split", {}).get("code", "")
                s    = split.get("stat", {})
                pa   = int(s.get("plateAppearances", 0) or 0)
                k    = int(s.get("strikeOuts", 0) or 0)
                bb   = int(s.get("baseOnBalls", 0) or 0)
                entry = {
                    "pa":    pa,
                    "ab":    int(s.get("atBats", 0) or 0),
                    "ba":    s.get("avg", "---"),
                    "obp":   s.get("obp", "---"),
                    "slg":   s.get("slg", "---"),
                    "ops":   s.get("ops", "---"),
                    "hr":    int(s.get("homeRuns", 0) or 0),
                    "k_pct": f"{k/pa*100:.1f}%" if pa > 0 else "---",
                    "bb_pct":f"{bb/pa*100:.1f}%" if pa > 0 else "---",
                }
                if code == "vl":
                    result["vs_lhp"] = entry    # batter vs left-handed pitcher
                elif code == "vr":
                    result["vs_rhp"] = entry    # batter vs right-handed pitcher
        if result:
            result["player_id"] = player_id
    except Exception as exc:
        log.debug("fetch_mlb_batter_platoon_splits failed for %s: %s", player_name, exc)
    return result


def _fetch_nhl_player_id(player_name: str) -> Optional[int]:
    """
    Look up an NHL player ID via the NHL search endpoint.
    Returns None if not found.
    """
    if not player_name:
        return None
    data = _get(_NHL_PLAYER_SEARCH, params={
        "q":     player_name,
        "type":  "player",
        "limit": 3,
    })
    # Response is a list of hits
    hits = data if isinstance(data, list) else []
    for hit in hits:
        pid = hit.get("playerId") or hit.get("id")
        if pid:
            return int(pid)
    return None


def fetch_nhl_goalie_stats(player_name: str) -> dict:
    """
    Return season and recent stats for an NHL goaltender by name.

    Keys:
        save_pct, gaa, wins, losses, ot_losses, games_played,
        shots_against_per_gm, recent (list of last-5 game dicts with
        date, opponent, decision, save_pct, shots_against)

    Returns {} on any failure.
    """
    pid = _fetch_nhl_player_id(player_name)
    if not pid:
        log.debug("fetch_nhl_goalie_stats: player not found: %s", player_name)
        return {}

    landing = _get(f"{_NHL_PLAYER_LAND}/{pid}/landing")
    if not landing:
        return {}

    result: dict = {}
    try:
        fs   = landing.get("featuredStats", {})
        reg  = fs.get("regularSeason", {}).get("subSeason", {})
        gp   = int(reg.get("gamesPlayed", 0) or 0)
        sa   = int(reg.get("shotsAgainst", 0) or 0)
        result = {
            "player_name":        landing.get("fullName") or player_name,
            "position":           landing.get("position", "G"),
            "games_played":       gp,
            "wins":               int(reg.get("wins", 0) or 0),
            "losses":             int(reg.get("losses", 0) or 0),
            "ot_losses":          int(reg.get("otLosses", 0) or 0),
            "save_pct":           round(float(reg.get("savePctg", 0) or 0), 3),
            "gaa":                round(float(reg.get("goalsAgainstAvg", 0) or 0), 2),
            "shots_against_per_gm": round(sa / gp, 1) if gp > 0 else None,
            "shutouts":           int(reg.get("shutouts", 0) or 0),
        }
    except Exception as exc:
        log.debug("fetch_nhl_goalie_stats: landing parse failed for %s: %s", player_name, exc)
        return {}

    # Fetch last-5 game log for recent form
    try:
        gl = _get(f"{_NHL_PLAYER_LAND}/{pid}/game-log/now")
        games = (gl.get("gameLog") or [])[:5]
        recent = []
        for g in games:
            svpct_raw = g.get("savePctg") or g.get("savePercentage")
            sa_g = int(g.get("shotsAgainst", 0) or 0)
            ga_g = int(g.get("goalsAgainst", 0) or 0)
            recent.append({
                "date":          g.get("gameDate", ""),
                "opponent":      g.get("opponentAbbrev", ""),
                "home_or_away":  "H" if g.get("homeRoadFlag", "").upper() == "H" else "A",
                "decision":      g.get("decision", ""),
                "shots_against": sa_g,
                "goals_against": ga_g,
                "save_pct":      round(float(svpct_raw), 3) if svpct_raw else (
                    round((sa_g - ga_g) / sa_g, 3) if sa_g > 0 else None
                ),
            })
        result["recent"] = recent
    except Exception as exc:
        log.debug("fetch_nhl_goalie_stats: gamelog failed for %s: %s", player_name, exc)
        result["recent"] = []

    return result


def fetch_nhl_player_sog_avg(player_name: str) -> dict:
    """
    Return recent shots-on-goal averages for an NHL skater.

    Keys:
        player_name, season_sog_per_gm, last5_sog_per_gm, last10_sog_per_gm,
        recent (list of last-10 game dicts with date, opponent, sog, goals)

    Returns {} on any failure.
    """
    pid = _fetch_nhl_player_id(player_name)
    if not pid:
        log.debug("fetch_nhl_player_sog_avg: player not found: %s", player_name)
        return {}

    landing = _get(f"{_NHL_PLAYER_LAND}/{pid}/landing")
    gl_data = _get(f"{_NHL_PLAYER_LAND}/{pid}/game-log/now")

    result: dict = {"player_name": player_name}

    # Season SOG per game from landing
    try:
        fs  = landing.get("featuredStats", {})
        reg = fs.get("regularSeason", {}).get("subSeason", {})
        gp  = int(reg.get("gamesPlayed", 0) or 0)
        sog = int(reg.get("shots", 0) or 0)
        result["season_sog_per_gm"] = round(sog / gp, 1) if gp > 0 else None
        result["season_games"]      = gp
    except Exception:
        result["season_sog_per_gm"] = None

    # Recent game log
    try:
        games = gl_data.get("gameLog") or []
        recent = []
        for g in games[:10]:
            recent.append({
                "date":         g.get("gameDate", ""),
                "opponent":     g.get("opponentAbbrev", ""),
                "home_or_away": "H" if g.get("homeRoadFlag", "").upper() == "H" else "A",
                "sog":          int(g.get("shots", g.get("shotsOnGoal", 0)) or 0),
                "goals":        int(g.get("goals", 0) or 0),
                "toi":          g.get("toi", ""),
            })

        def _avg_sog(n: int) -> Optional[float]:
            subset = [r["sog"] for r in recent[:n] if r["sog"] is not None]
            return round(sum(subset) / len(subset), 1) if subset else None

        result["last5_sog_per_gm"]  = _avg_sog(5)
        result["last10_sog_per_gm"] = _avg_sog(10)
        result["recent"] = recent
    except Exception as exc:
        log.debug("fetch_nhl_player_sog_avg: gamelog failed for %s: %s", player_name, exc)
        result["recent"] = []

    return result


def fetch_prop_context(
    player_name: str,
    prop_market: str,
    sport_key: str,
    pitcher_id: Optional[int] = None,
) -> dict:
    """
    Main dispatcher: return prop-specific context for a given player and market.

    Routes:
        baseball_mlb + pitcher_*  → pitcher platoon splits (vs LHB / vs RHB)
        baseball_mlb + batter_*   → batter platoon splits (vs LHP / vs RHP)
        icehockey_nhl + player_shots_on_goal  → SOG averages
        icehockey_nhl + player_goals/assists/points/blocked_shots → goalie stats

    Returns a dict with a "prop_context_type" key describing what's in it.
    """
    ctx: dict = {
        "prop_context_type": "none",
        "player_name":       player_name,
        "prop_market":       prop_market,
        "sport_key":         sport_key,
    }

    if sport_key == "baseball_mlb":
        _PITCHER_MARKETS = {
            "pitcher_strikeouts", "pitcher_hits_allowed", "pitcher_earned_runs",
        }
        _BATTER_MARKETS = {
            "batter_home_runs", "batter_hits", "batter_rbis",
            "batter_total_bases", "batter_strikeouts",
        }

        if prop_market in _PITCHER_MARKETS:
            # Resolve pitcher ID: prefer pre-fetched ID, fall back to search
            pid = pitcher_id or _fetch_mlb_player_id(player_name)
            if pid:
                splits = fetch_mlb_pitcher_platoon_splits(pid)
                ctx.update(splits)
                ctx["pitcher_id"]        = pid
                ctx["prop_context_type"] = "mlb_pitcher_splits"
            else:
                ctx["prop_context_type"] = "no_data"

        elif prop_market in _BATTER_MARKETS:
            splits = fetch_mlb_batter_platoon_splits(player_name)
            if splits:
                ctx.update(splits)
                ctx["prop_context_type"] = "mlb_batter_splits"
            else:
                ctx["prop_context_type"] = "no_data"

    elif sport_key == "icehockey_nhl":
        if prop_market == "player_shots_on_goal":
            data = fetch_nhl_player_sog_avg(player_name)
            if data:
                ctx.update(data)
                ctx["prop_context_type"] = "nhl_player_sog"
            else:
                ctx["prop_context_type"] = "no_data"
        else:
            # Goals, assists, points, blocked shots — show goalie context
            data = fetch_nhl_goalie_stats(player_name)
            if data:
                ctx.update(data)
                ctx["prop_context_type"] = "nhl_goalie"
            else:
                ctx["prop_context_type"] = "no_data"

    return ctx


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
            form     = fetch_nhl_team_form()

            all_names = set(goalies) | set(splits) | set(injuries) | set(form)
            ctx: dict = {}
            for name in all_names:
                f = form.get(name, {})
                ctx[_normalise(name)] = {
                    "home_win_pct":     splits.get(name, {}).get("home_win_pct", 0.5),
                    "away_win_pct":     splits.get(name, {}).get("away_win_pct", 0.5),
                    "goalie_confirmed": goalies.get(name, {}).get("confirmed", None),
                    "goalie_name":      goalies.get(name, {}).get("starter"),
                    "injuries":         injuries.get(name, []),
                    "b2b":              False,
                    "last10_wins":      f.get("last10_wins"),   # int 0-10, None if unavailable
                    "streak_val":       f.get("streak_val", 0),
                }
            log.info("build_context(nhl): %d teams populated", len(ctx))
            return ctx

        elif sport_key == "basketball_nba":
            b2b      = fetch_nba_b2b()
            splits   = fetch_nba_home_away_splits()
            injuries = fetch_nba_injuries()
            form     = fetch_nba_team_form()

            all_names = set(b2b) | set(splits) | set(injuries) | set(form)
            ctx = {}
            for name in all_names:
                f = form.get(name, {})
                ctx[_normalise(name)] = {
                    "home_win_pct":     splits.get(name, {}).get("home_win_pct", 0.5),
                    "away_win_pct":     splits.get(name, {}).get("away_win_pct", 0.5),
                    "goalie_confirmed": None,
                    "goalie_name":      None,
                    "injuries":         injuries.get(name, []),
                    "b2b":              bool(b2b.get(name, False)),
                    "last10_wins":      f.get("last10_wins"),   # int 0-10, None if unavailable
                    "streak_val":       f.get("streak_val", 0),
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
            "  (proj_item->>'mean')::float  AS mean, "
            "  (proj_item->>'p25')::float   AS p25, "
            "  (proj_item->>'p75')::float   AS p75 "
            "FROM events e "
            "JOIN game_projections gp ON gp.event_id = e.id, "
            "  LATERAL jsonb_array_elements(gp.projections->'projections') AS proj_item "
            f"WHERE e.league = '{opt_league}' "
            f"  AND e.start_date > NOW() - INTERVAL '4 hours' "
            f"  AND e.start_date < NOW() + INTERVAL '48 hours' "
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

            # Pivot projection rows — capture mean + percentile bands (p25/p75)
            pt   = row.get("proj_type", "")
            mean = row.get("mean")
            p25  = row.get("p25")
            p75  = row.get("p75")
            if pt == "spread":
                result["spread_mean"] = mean
                if p25 is not None: result["spread_p25"] = p25
                if p75 is not None: result["spread_p75"] = p75
            elif pt == "total":
                result["total_mean"] = mean
                if p25 is not None: result["total_p25"] = p25
                if p75 is not None: result["total_p75"] = p75
            elif pt == "homeScore":
                result["home_score_mean"] = mean
            elif pt == "awayScore":
                result["away_score_mean"] = mean

        if "spread_mean" not in result and "total_mean" not in result:
            log.debug("fetch_game_projections: matched rows but no projection data for %s", game)
            # Fall through to MLB fallback below
        else:
            # Derive individual team scores from spread + total when Optimal doesn't
            # provide homeScore/awayScore directly (common for NHL, soccer).
            # home_score = (total + spread) / 2,  away_score = (total - spread) / 2
            # where spread_mean is the home team's projected margin (positive = home wins by that much)
            if (
                result.get("home_score_mean") is None
                and result.get("total_mean") is not None
                and result.get("spread_mean") is not None
            ):
                _t = float(result["total_mean"])
                _s = float(result["spread_mean"])
                result["home_score_mean"] = round((_t + _s) / 2, 2)
                result["away_score_mean"] = round((_t - _s) / 2, 2)
                log.debug(
                    "fetch_game_projections: derived scores for %s → home=%.2f away=%.2f",
                    game, result["home_score_mean"], result["away_score_mean"],
                )

            log.info(
                "fetch_game_projections: %s spread=%.2f total=%.2f home=%.2f away=%.2f home_win=%.1f%%",
                game,
                result.get("spread_mean", 0),
                result.get("total_mean", 0),
                result.get("home_score_mean") or 0,
                result.get("away_score_mean") or 0,
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


def fetch_game_context(game: str, league: str, commence_dt=None) -> dict:
    """
    Fetch contextual betting data for a specific game:
    team records, streaks, last 10 games, playoff position/note,
    key injuries, starting goalie (NHL), probable pitcher (MLB).

    Returns {} on failure — never raises.
    """
    if " @ " not in game:
        return {}

    _SPORT_ESPN = {
        "icehockey_nhl":              "hockey/nhl",
        "basketball_nba":             "basketball/nba",
        "baseball_mlb":               "baseball/mlb",
        "soccer_epl":                 "soccer/eng.1",
        "soccer_spain_la_liga":       "soccer/esp.1",
        "soccer_germany_bundesliga":  "soccer/ger.1",
        "soccer_usa_mls":             "soccer/usa.1",
    }

    sport_path = _SPORT_ESPN.get(league)
    if not sport_path:
        return {}

    away_name, home_name = [s.strip() for s in game.split(" @ ", 1)]

    # Game date in ET (US sports use ET scheduling)
    if commence_dt:
        try:
            from zoneinfo import ZoneInfo
            dt_et = commence_dt.astimezone(ZoneInfo("America/New_York"))
            date_str = dt_et.strftime("%Y%m%d")
        except Exception:
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    result: dict = {"away_team": away_name, "home_team": home_name}

    def _overlap(a: str, b: str) -> bool:
        skip = {"at", "the", "a", "an", "city", "state", "fc", "sc",
                "united", "de", "los", "san", "new", "red", "bay"}
        wa = {w for w in a.lower().split() if w not in skip and len(w) > 2}
        wb = {w for w in b.lower().split() if w not in skip and len(w) > 2}
        return bool(wa & wb)

    # ── ESPN scoreboard: records + streaks ───────────────────────────────
    try:
        board = _get(
            f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard",
            params={"dates": date_str},
        )
        matched = None
        for event in board.get("events", []):
            for comp in event.get("competitions", []):
                comps = comp.get("competitors", [])
                away_e = next((c.get("team", {}).get("displayName", "")
                               for c in comps if c.get("homeAway") == "away"), "")
                home_e = next((c.get("team", {}).get("displayName", "")
                               for c in comps if c.get("homeAway") == "home"), "")
                if _overlap(away_e, away_name) and _overlap(home_e, home_name):
                    matched = comp
                    break
            if matched:
                break

        if matched:
            for c in matched.get("competitors", []):
                side = c.get("homeAway", "")          # "home" or "away"
                recs  = {r.get("type"): r.get("summary", "")
                         for r in c.get("records", [])}
                result[f"{side}_record"] = recs.get("total", "")
                sk = c.get("streak", {})
                if sk:
                    result[f"{side}_streak"] = (
                        sk.get("shortDisplayValue") or sk.get("displayValue", "")
                    )
                # Capture team ID for manager lookup below
                team_id = c.get("team", {}).get("id")
                if team_id:
                    result[f"_{side}_team_id"] = team_id   # internal, stripped later
            notes = [n.get("headline", "")
                     for n in matched.get("notes", []) if n.get("headline")]
            if notes:
                result["game_notes"] = notes
    except Exception as _e:
        log.debug("fetch_game_context: ESPN scoreboard error: %s", _e)

    # ── Soccer: current manager via ESPN teams endpoint ───────────────────
    # Fetch live — never rely on Claude's training data for coaching staff.
    if league.startswith("soccer_") and sport_path:
        for side in ("home", "away"):
            team_id_key = f"_{side}_team_id"
            team_id = result.pop(team_id_key, None)   # remove internal key

            # Fallback: look up team ID from the full teams list if scoreboard didn't match
            if not team_id:
                try:
                    teams_data = _get(
                        f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams"
                    )
                    name_to_check = home_name if side == "home" else away_name
                    for entry in teams_data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
                        t = entry.get("team", {})
                        disp = t.get("displayName", "")
                        if _overlap(disp, name_to_check):
                            team_id = t.get("id")
                            break
                except Exception as _te:
                    log.debug("fetch_game_context: ESPN teams list error (%s): %s", side, _te)

            if not team_id:
                continue
            try:
                team_data = _get(
                    f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{team_id}"
                )
                coaches = team_data.get("team", {}).get("coaches", [])
                manager = None
                for co in coaches:
                    pos = (co.get("position", {}) or {}).get("name", "").lower()
                    if pos in ("manager", "head coach", "coach", "first-team manager"):
                        first = co.get("firstName", "")
                        last  = co.get("lastName", "")
                        manager = f"{first} {last}".strip()
                        break
                if not manager and coaches:
                    # Fallback: first listed coach
                    co = coaches[0]
                    manager = f"{co.get('firstName', '')} {co.get('lastName', '')}".strip()
                if manager:
                    result[f"{side}_manager"] = manager
                    log.debug("fetch_game_context: %s manager=%s (%s)", side, manager, team_id)
            except Exception as _me:
                log.debug("fetch_game_context: ESPN manager fetch error (%s team_id=%s): %s", side, team_id, _me)

    # ── NHL: standings (points, last-10, playoff position/clinch) ────────
    if league == "icehockey_nhl":
        try:
            standings = _get(_NHL_STANDINGS)
            away_kw = away_name.split()[-1].lower()
            home_kw = home_name.split()[-1].lower()

            for entry in standings.get("standings", []):
                tname = (
                    entry.get("teamName", {}).get("default", "")
                    or entry.get("teamCommonName", {}).get("default", "")
                    or entry.get("teamAbbrev", {}).get("default", "")
                ).lower()
                if not tname:
                    continue

                if away_kw in tname:
                    side = "away"
                elif home_kw in tname:
                    side = "home"
                else:
                    continue

                result[f"{side}_pts"]        = entry.get("points", 0)
                result[f"{side}_conf_rank"]  = entry.get("conferenceSequence", 0)
                result[f"{side}_wc_rank"]    = entry.get("wildcardSequence", 0)
                l10 = (f"{entry.get('l10Wins',0)}-"
                       f"{entry.get('l10Losses',0)}-"
                       f"{entry.get('l10OtLosses',0)}")
                result[f"{side}_last10"]     = l10

                ci = entry.get("clinchIndicator", "")
                playoff_note = {
                    "p": "Clinched playoff berth",
                    "z": "Clinched division",
                    "y": "Clinched conference",
                    "x": "Clinched Presidents' Trophy",
                    "e": "Eliminated from playoffs",
                }.get(ci, "")
                if playoff_note:
                    result[f"{side}_playoff_note"] = playoff_note
        except Exception as _e:
            log.debug("fetch_game_context: NHL standings error: %s", _e)

        # Goalie
        try:
            goalies = fetch_nhl_goalies()
            away_kw2 = away_name.split()[-1]
            home_kw2 = home_name.split()[-1]
            for gname, ginfo in goalies.items():
                gname_l = gname.lower()
                if away_kw2.lower() in gname_l:
                    result["away_goalie"]           = ginfo.get("starter") or "TBD"
                    result["away_goalie_confirmed"] = ginfo.get("confirmed", False)
                elif home_kw2.lower() in gname_l:
                    result["home_goalie"]           = ginfo.get("starter") or "TBD"
                    result["home_goalie_confirmed"] = ginfo.get("confirmed", False)
        except Exception:
            pass

        # Injuries
        try:
            inj = fetch_nhl_injuries()
            away_kw2 = away_name.split()[-1]
            home_kw2 = home_name.split()[-1]
            for tname, players in inj.items():
                if not players:
                    continue
                tl = tname.lower()
                if away_kw2.lower() in tl:
                    result["away_injuries"] = players[:6]
                elif home_kw2.lower() in tl:
                    result["home_injuries"] = players[:6]
        except Exception:
            pass

    # ── NBA: playoff seed + injuries ─────────────────────────────────────
    elif league == "basketball_nba":
        try:
            sd = _get(_ESPN_NBA_STAND)
            away_kw = away_name.split()[-1].lower()
            home_kw = home_name.split()[-1].lower()
            all_entries = []
            for child in sd.get("children", []):
                all_entries.extend(
                    child.get("standings", {}).get("entries", []))
            for entry in all_entries:
                tname = entry.get("team", {}).get("displayName", "").lower()
                stats = {s.get("name"): s for s in entry.get("stats", [])}
                if away_kw in tname:
                    side = "away"
                elif home_kw in tname:
                    side = "home"
                else:
                    continue
                if not result.get(f"{side}_record"):
                    ov = stats.get("overall", {})
                    result[f"{side}_record"] = ov.get("displayValue", "")
                ps = stats.get("playoffSeed", {})
                if ps:
                    result[f"{side}_playoff_seed"] = int(ps.get("value", 0) or 0)
                l10 = stats.get("Last Ten Games", {}) or stats.get("L10", {})
                if l10:
                    result[f"{side}_last10"] = l10.get("displayValue", "")
        except Exception as _e:
            log.debug("fetch_game_context: NBA standings error: %s", _e)

        try:
            inj = fetch_nba_injuries()
            away_kw = away_name.split()[-1]
            home_kw = home_name.split()[-1]
            for tname, players in inj.items():
                if not players:
                    continue
                tl = tname.lower()
                if away_kw.lower() in tl:
                    result["away_injuries"] = players[:6]
                elif home_kw.lower() in tl:
                    result["home_injuries"] = players[:6]
        except Exception:
            pass

    # ── MLB: probable pitchers ────────────────────────────────────────────
    elif league == "baseball_mlb":
        try:
            pit_date = (commence_dt.strftime("%Y-%m-%d")
                        if commence_dt
                        else datetime.now(timezone.utc).strftime("%Y-%m-%d"))
            sched = _get(
                _MLB_SCHEDULE,
                params={"sportId": 1, "date": pit_date,
                        "hydrate": "probablePitcher"},
            )
            away_kw = away_name.split()[-1].lower()
            home_kw = home_name.split()[-1].lower()
            for de in sched.get("dates", []):
                for g in de.get("games", []):
                    teams = g.get("teams", {})
                    ht = teams.get("home", {}).get("team", {}).get("name", "").lower()
                    at = teams.get("away", {}).get("team", {}).get("name", "").lower()
                    if home_kw in ht and away_kw in at:
                        for side in ("home", "away"):
                            pp = teams.get(side, {}).get("probablePitcher")
                            if pp:
                                result[f"{side}_pitcher"] = pp.get("fullName", "TBD")
                        break
        except Exception:
            pass

    log.debug("fetch_game_context: %s → %d context fields", game, len(result))
    return result


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
