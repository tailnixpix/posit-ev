"""
scripts/espn_history.py — free team results history from ESPN's public site API.

Powers the "history" prong of the newsletter pick test: recent form, venue
split, results against today's spread / total, and head-to-head meetings.
No API key, no credits. Results are cached in-process so a day's picks cost a
handful of requests.
"""
import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Odds API sport key -> ESPN "sport/league" path
ESPN_PATHS = {
    "americanfootball_nfl":      "football/nfl",
    "americanfootball_ncaaf":    "football/college-football",
    "americanfootball_cfl":      "football/cfl",
    "basketball_nba":            "basketball/nba",
    "basketball_ncaab":          "basketball/mens-college-basketball",
    "icehockey_nhl":             "hockey/nhl",
    "baseball_mlb":              "baseball/mlb",
    "soccer_epl":                "soccer/eng.1",
    "soccer_spain_la_liga":      "soccer/esp.1",
    "soccer_germany_bundesliga": "soccer/ger.1",
    "soccer_usa_mls":            "soccer/usa.1",
    "soccer_uefa_champs_league": "soccer/uefa.champions",
    "soccer_fifa_world_cup":     "soccer/fifa.world",
}

_TTL_TEAMS = 24 * 3600
_TTL_RESULTS = 3 * 3600
_cache: dict = {}


def _get_json(url: str, params: dict = None) -> Optional[dict]:
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=8)
            if r.status_code == 200:
                return r.json()
            if r.status_code < 500:
                return None
        except requests.RequestException as exc:
            log.debug("ESPN request failed (%s): %s", url, exc)
        time.sleep(1)
    return None


def _cached(key, ttl, loader):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = loader()
    _cache[key] = (time.time(), val)
    return val


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _teams(sport_key: str) -> dict:
    """{normalised displayName: team_id} for a league."""
    path = ESPN_PATHS.get(sport_key)
    if not path:
        return {}

    def load():
        d = _get_json(f"{_BASE}/{path}/teams", {"limit": 500}) or {}
        try:
            teams = d["sports"][0]["leagues"][0]["teams"]
        except (KeyError, IndexError):
            return {}
        out = {}
        for t in teams:
            t = t["team"]
            for nm in (t.get("displayName"), t.get("name"), t.get("shortDisplayName")):
                if nm:
                    out.setdefault(_norm(nm), t["id"])
        return out

    return _cached(("teams", sport_key), _TTL_TEAMS, load)


def find_team_id(sport_key: str, team_name: str) -> Optional[str]:
    teams = _teams(sport_key)
    n = _norm(team_name)
    if n in teams:
        return teams[n]
    # Loose containment either way ("Ohio State" vs "Ohio State Buckeyes")
    for k, tid in teams.items():
        if n and (n in k or k in n) and min(len(n), len(k)) >= 5:
            return tid
    return None


def _results(sport_key: str, team_id: str, season: Optional[int] = None) -> list:
    """Completed games for a team, newest first."""
    path = ESPN_PATHS[sport_key]

    def load():
        params = {"season": season} if season else None
        d = _get_json(f"{_BASE}/{path}/teams/{team_id}/schedule", params) or {}
        out = []
        for ev in d.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            me = opp = None
            for c in comp.get("competitors", []):
                if str(c.get("team", {}).get("id")) == str(team_id):
                    me = c
                else:
                    opp = c
            if not me or not opp:
                continue
            try:
                pf = float(me["score"]["value"])
                pa = float(opp["score"]["value"])
            except (KeyError, TypeError, ValueError):
                continue
            out.append({
                "date": ev.get("date", "")[:10],
                "home": me.get("homeAway") == "home",
                "opp_id": str(opp["team"]["id"]),
                "opp": opp["team"].get("displayName", ""),
                "pf": pf, "pa": pa,
                "season": (ev.get("season") or {}).get("year"),
            })
        out.sort(key=lambda g: g["date"], reverse=True)
        return out

    return _cached(("results", sport_key, team_id, season), _TTL_RESULTS, load)


def team_results(sport_key: str, team_name: str, with_prior_season: bool = False) -> Optional[dict]:
    """
    {"team_id", "games": [newest-first completed games]} or None when the team
    can't be resolved. with_prior_season appends last season's games (used for
    head-to-head, where a single season may hold 0–1 meetings).
    """
    if sport_key not in ESPN_PATHS:
        return None
    tid = find_team_id(sport_key, team_name)
    if not tid:
        return None
    games = list(_results(sport_key, tid))
    if with_prior_season and games and games[0].get("season"):
        games += _results(sport_key, tid, int(games[0]["season"]) - 1)
    elif with_prior_season and not games:
        pass
    return {"team_id": tid, "games": games}
