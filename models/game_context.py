"""
models/game_context.py — Real-world game context enrichment

Fetches per-game context to enrich card summaries with factual, current data:
  1. Injury / availability  (ESPN undocumented API — NHL, NBA, MLB, NFL)
  2. Rest / back-to-back    (ESPN schedule — NHL, NBA, MLB, NFL)
  3. Weather forecast       (Open-Meteo — NFL and MLB outdoor games only)
  4. Team pace / efficiency (ESPN team statistics)

All functions fail gracefully — network or parse errors return empty data and
never raise to the caller.  The main entry point is enrich_game().
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 8  # seconds per HTTP call

# ---------------------------------------------------------------------------
# ESPN sport/league slug map
# ---------------------------------------------------------------------------

_ESPN_MAP: dict[str, tuple[str, str]] = {
    "icehockey_nhl":        ("hockey",     "nhl"),
    "basketball_nba":       ("basketball", "nba"),
    "baseball_mlb":         ("baseball",   "mlb"),
    "americanfootball_nfl": ("football",   "nfl"),
}

OUTDOOR_LEAGUES: frozenset[str] = frozenset({"americanfootball_nfl", "baseball_mlb"})

# ---------------------------------------------------------------------------
# Stadium coordinates for Open-Meteo weather lookup (outdoor venues only)
# ---------------------------------------------------------------------------

_STADIUM_COORDS: dict[str, tuple[float, float]] = {
    # ── NFL ─────────────────────────────────────────────────────────────────
    "arizona cardinals":     (33.5277, -112.2626),
    "atlanta falcons":       (33.7554,  -84.4009),
    "baltimore ravens":      (39.2780,  -76.6227),
    "buffalo bills":         (42.7738,  -78.7870),
    "carolina panthers":     (35.2258,  -80.8531),
    "chicago bears":         (41.8623,  -87.6167),
    "cincinnati bengals":    (39.0955,  -84.5160),
    "cleveland browns":      (41.5061,  -81.6995),
    "dallas cowboys":        (32.7474,  -97.0945),
    "denver broncos":        (39.7439, -105.0201),
    "detroit lions":         (42.3400,  -83.0456),
    "green bay packers":     (44.5013,  -88.0622),
    "houston texans":        (29.6847,  -95.4107),
    "indianapolis colts":    (39.7601,  -86.1638),
    "jacksonville jaguars":  (30.3240,  -81.6373),
    "kansas city chiefs":    (39.0489,  -94.4839),
    "las vegas raiders":     (36.0908, -115.1836),
    "los angeles chargers":  (33.9535, -118.3392),
    "los angeles rams":      (33.9535, -118.3392),
    "miami dolphins":        (25.9580,  -80.2389),
    "minnesota vikings":     (44.9737,  -93.2571),
    "new england patriots":  (42.0909,  -71.2643),
    "new orleans saints":    (29.9511,  -90.0812),
    "new york giants":       (40.8135,  -74.0745),
    "new york jets":         (40.8135,  -74.0745),
    "philadelphia eagles":   (39.9008,  -75.1675),
    "pittsburgh steelers":   (40.4468,  -80.0158),
    "san francisco 49ers":   (37.4033, -121.9694),
    "seattle seahawks":      (47.5952, -122.3316),
    "tampa bay buccaneers":  (27.9759,  -82.5033),
    "tennessee titans":      (36.1665,  -86.7713),
    "washington commanders": (38.9077,  -76.8645),
    # ── MLB ─────────────────────────────────────────────────────────────────
    "arizona diamondbacks":  (33.4453, -112.0667),
    "atlanta braves":        (33.8906,  -84.4681),
    "baltimore orioles":     (39.2839,  -76.6217),
    "boston red sox":        (42.3467,  -71.0972),
    "chicago cubs":          (41.9484,  -87.6553),
    "chicago white sox":     (41.8300,  -87.6338),
    "cincinnati reds":       (39.0979,  -84.5082),
    "cleveland guardians":   (41.4962,  -81.6852),
    "colorado rockies":      (39.7559, -104.9942),
    "detroit tigers":        (42.3390,  -83.0485),
    "houston astros":        (29.7573,  -95.3555),
    "kansas city royals":    (39.0517,  -94.4803),
    "los angeles angels":    (33.8003, -117.8827),
    "los angeles dodgers":   (34.0739, -118.2400),
    "miami marlins":         (25.7781,  -80.2197),
    "milwaukee brewers":     (43.0281,  -87.9712),
    "minnesota twins":       (44.9818,  -93.2777),
    "new york mets":         (40.7571,  -73.8458),
    "new york yankees":      (40.8296,  -73.9262),
    "oakland athletics":     (37.7516, -122.2005),
    "philadelphia phillies": (39.9061,  -75.1665),
    "pittsburgh pirates":    (40.4469,  -80.0057),
    "san diego padres":      (32.7076, -117.1570),
    "san francisco giants":  (37.7786, -122.3893),
    "seattle mariners":      (47.5914, -122.3325),
    "st. louis cardinals":   (38.6226,  -90.1928),
    "tampa bay rays":        (27.7683,  -82.6534),
    "texas rangers":         (32.7512,  -97.0832),
    "toronto blue jays":     (43.6414,  -79.3894),
    "washington nationals":  (38.8730,  -77.0074),
}


def _stadium_coords(team_name: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) for the team's home stadium, or None if unknown."""
    name = team_name.lower().strip()
    if name in _STADIUM_COORDS:
        return _STADIUM_COORDS[name]
    # Fuzzy: match on nickname (last word)
    parts = name.split()
    if parts:
        last = parts[-1]
        for key, coords in _STADIUM_COORDS.items():
            if key.split()[-1] == last:
                return coords
    return None


# ---------------------------------------------------------------------------
# ESPN team ID cache (in-process, keyed by "sport:league")
# ---------------------------------------------------------------------------

_team_id_cache: dict[str, dict[str, str]] = {}


def _espn_team_id(sport: str, league: str, team_name: str) -> Optional[str]:
    """
    Resolve an ESPN numeric team ID for the given team name.
    Results are cached in-process.  Returns None on any failure.
    """
    cache_key = f"{sport}:{league}"
    if cache_key not in _team_id_cache:
        try:
            url = (
                f"https://site.api.espn.com/apis/site/v2/sports"
                f"/{sport}/{league}/teams?limit=200"
            )
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            teams_list = (
                data.get("sports", [{}])[0]
                    .get("leagues", [{}])[0]
                    .get("teams", [])
            )
            mapping: dict[str, str] = {}
            for entry in teams_list:
                t = entry.get("team", {})
                tid = str(t.get("id", ""))
                if not tid:
                    continue
                for field in ("displayName", "shortDisplayName", "name", "abbreviation"):
                    val = (t.get(field) or "").lower().strip()
                    if val:
                        mapping[val] = tid
            _team_id_cache[cache_key] = mapping
        except Exception as exc:
            log.debug("_espn_team_id: teams load failed for %s/%s: %s", sport, league, exc)
            _team_id_cache[cache_key] = {}

    mapping = _team_id_cache.get(cache_key, {})
    name_lower = team_name.lower().strip()

    # 1. Exact match
    if name_lower in mapping:
        return mapping[name_lower]

    # 2. Nickname (last word) match
    parts = name_lower.split()
    if parts:
        last = parts[-1]
        for key, tid in mapping.items():
            if key == last or key.endswith(" " + last):
                return tid

    log.debug("_espn_team_id: no match for '%s' in %s/%s", team_name, sport, league)
    return None


# ---------------------------------------------------------------------------
# 1. Injuries
# ---------------------------------------------------------------------------

def _fetch_injuries(sport: str, league: str, team_id: str) -> list[dict]:
    """
    Return list of injury dicts: {player, status, type}.
    Returns empty list on any failure.
    """
    try:
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}"
            f"/teams/{team_id}?enable=injuries"
        )
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("team", {}).get("injuries", [])
        result = []
        for inj in raw:
            athlete = inj.get("athlete", {})
            player = (
                athlete.get("displayName")
                or athlete.get("fullName")
                or "Unknown"
            )
            status = (inj.get("status") or "Unknown").strip()
            inj_type = (inj.get("type") or {}).get("text", "")
            result.append({"player": player, "status": status, "type": inj_type})
        return result
    except Exception as exc:
        log.debug("_fetch_injuries(%s/%s id=%s): %s", sport, league, team_id, exc)
        return []


# ---------------------------------------------------------------------------
# 2. Rest / back-to-back
# ---------------------------------------------------------------------------

def _last_game_date(sport: str, league: str, team_id: str, before: datetime) -> Optional[datetime]:
    """
    Return the datetime of the team's most recent completed game before `before`.
    Returns None if unavailable.
    """
    try:
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}"
            f"/teams/{team_id}/schedule"
        )
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])

        completed: list[datetime] = []
        for event in events:
            comps = event.get("competitions", [])
            if not any(
                c.get("status", {}).get("type", {}).get("completed", False)
                for c in comps
            ):
                continue
            date_str = event.get("date", "")
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt < before:
                    completed.append(dt)
            except Exception:
                continue

        return max(completed) if completed else None
    except Exception as exc:
        log.debug("_last_game_date(%s/%s id=%s): %s", sport, league, team_id, exc)
        return None


def _days_rest(sport: str, league: str, team_id: str, commence_time: datetime) -> Optional[int]:
    """Return whole days of rest (0 = back-to-back). None if data unavailable."""
    last = _last_game_date(sport, league, team_id, commence_time)
    if last is None:
        return None
    return max(0, (commence_time - last).days)


# ---------------------------------------------------------------------------
# 3. Weather (Open-Meteo — no API key required)
# ---------------------------------------------------------------------------

def _fetch_weather(lat: float, lon: float, game_time: datetime) -> Optional[dict]:
    """
    Return weather dict {temp_f, wind_mph, precip_pct, summary} for the
    given location/time.  Returns None on failure or if data is out of range.
    """
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,precipitation_probability,windspeed_10m"
            "&temperature_unit=fahrenheit&windspeed_unit=mph"
            "&forecast_days=10&timezone=UTC"
        )
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})

        times  = hourly.get("time", [])
        temps  = hourly.get("temperature_2m", [])
        precip = hourly.get("precipitation_probability", [])
        winds  = hourly.get("windspeed_10m", [])

        if not times:
            return None

        # Target: the hour slot closest to game_time (UTC, naive)
        gt = game_time.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        )

        best_idx, best_diff = 0, None
        for i, t_str in enumerate(times):
            try:
                t = datetime.fromisoformat(t_str)
                diff = abs((t - gt).total_seconds())
                if best_diff is None or diff < best_diff:
                    best_diff, best_idx = diff, i
            except Exception:
                continue

        # Skip if closest slot is more than 24 h away (game too far out)
        if best_diff is not None and best_diff > 86_400:
            return None

        def _safe(lst, idx):
            return lst[idx] if idx < len(lst) and lst[idx] is not None else None

        temp_f   = _safe(temps,  best_idx)
        wind_mph = _safe(winds,  best_idx)
        precip_p = _safe(precip, best_idx)

        if temp_f is None and wind_mph is None and precip_p is None:
            return None

        # Round values
        if temp_f   is not None: temp_f   = round(temp_f)
        if wind_mph is not None: wind_mph = round(wind_mph, 1)
        if precip_p is not None: precip_p = round(precip_p)

        parts = []
        if temp_f   is not None: parts.append(f"{temp_f}°F")
        if wind_mph is not None:
            label = " (significant)" if wind_mph >= 15 else ""
            parts.append(f"{wind_mph} mph wind{label}")
        if precip_p is not None: parts.append(f"{precip_p}% precip")

        return {
            "temp_f":     temp_f,
            "wind_mph":   wind_mph,
            "precip_pct": precip_p,
            "summary":    ", ".join(parts),
        }
    except Exception as exc:
        log.debug("_fetch_weather(%.3f, %.3f): %s", lat, lon, exc)
        return None


# ---------------------------------------------------------------------------
# 4. Pace / scoring efficiency
# ---------------------------------------------------------------------------

def _fetch_pace(sport: str, league: str, team_id: str) -> dict:
    """
    Return pace/efficiency metrics for the team.
      NBA → possessions per game (pace) + points per game
      NHL → goals for/against per game
      MLB → runs per game
    Returns empty dict on failure.
    """
    try:
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}"
            f"/teams/{team_id}/statistics"
        )
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # Flatten all stat categories into {name: value}
        stats: dict[str, float] = {}
        for cat in (
            data.get("results", {})
                .get("stats", {})
                .get("categories", [])
        ):
            for s in cat.get("stats", []):
                name = s.get("name", "")
                val  = s.get("value")
                if name and val is not None:
                    try:
                        stats[name] = float(val)
                    except (TypeError, ValueError):
                        pass

        result: dict = {}

        if league == "nba":
            for key in ("possessionsPerGame", "pace"):
                if key in stats:
                    result["pace"] = round(stats[key], 1)
                    break
            for key in ("pointsPerGame", "scoringAverage"):
                if key in stats:
                    result["pts_pg"] = round(stats[key], 1)
                    break

        elif league == "nhl":
            for key in ("goalsPerGame", "goalsForPerGame"):
                if key in stats:
                    result["goals_pg"] = round(stats[key], 2)
                    break
            if "goalsAgainstPerGame" in stats:
                result["goals_against_pg"] = round(stats["goalsAgainstPerGame"], 2)

        elif league == "mlb":
            for key in ("runsPerGame", "runsScoredPerGame"):
                if key in stats:
                    result["runs_pg"] = round(stats[key], 2)
                    break

        return result
    except Exception as exc:
        log.debug("_fetch_pace(%s/%s id=%s): %s", sport, league, team_id, exc)
        return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def enrich_game(
    league: str,
    home_team: str,
    away_team: str,
    commence_time: Optional[datetime],
) -> dict:
    """
    Fetch all available real-world context for a game.

    Returns a dict (possibly empty) with keys:
      injuries  → {home: [...], away: [...]}
      rest      → {home_days_rest, away_days_rest, home_b2b, away_b2b}
      weather   → {temp_f, wind_mph, precip_pct, summary}   (outdoor only)
      pace      → league-specific scoring/pace metrics
      fetched_at → ISO timestamp

    Never raises — all errors are caught and logged at DEBUG level.
    Safe to call from background threads.
    """
    try:
        if league not in _ESPN_MAP:
            return {}

        sport, espn_league = _ESPN_MAP[league]

        home_id = _espn_team_id(sport, espn_league, home_team) if home_team else None
        away_id = _espn_team_id(sport, espn_league, away_team) if away_team else None

        ctx: dict = {}

        # ── 1. Injuries ──────────────────────────────────────────────────────
        home_inj = _fetch_injuries(sport, espn_league, home_id) if home_id else []
        away_inj = _fetch_injuries(sport, espn_league, away_id) if away_id else []
        if home_inj or away_inj:
            ctx["injuries"] = {"home": home_inj, "away": away_inj}

        # ── 2. Rest / back-to-back ───────────────────────────────────────────
        if commence_time:
            ct = (
                commence_time
                if commence_time.tzinfo
                else commence_time.replace(tzinfo=timezone.utc)
            )
            home_rest = _days_rest(sport, espn_league, home_id, ct) if home_id else None
            away_rest = _days_rest(sport, espn_league, away_id, ct) if away_id else None
            if home_rest is not None or away_rest is not None:
                ctx["rest"] = {
                    "home_days_rest": home_rest,
                    "away_days_rest": away_rest,
                    "home_b2b":       home_rest == 0,
                    "away_b2b":       away_rest == 0,
                }

        # ── 3. Weather (outdoor only) ────────────────────────────────────────
        if league in OUTDOOR_LEAGUES and commence_time:
            coords = _stadium_coords(home_team)
            if coords:
                ct = (
                    commence_time
                    if commence_time.tzinfo
                    else commence_time.replace(tzinfo=timezone.utc)
                )
                weather = _fetch_weather(coords[0], coords[1], ct)
                if weather:
                    ctx["weather"] = weather

        # ── 4. Pace / efficiency ─────────────────────────────────────────────
        home_pace = _fetch_pace(sport, espn_league, home_id) if home_id else {}
        away_pace = _fetch_pace(sport, espn_league, away_id) if away_id else {}

        if home_pace or away_pace:
            pace_ctx: dict = {}
            if league == "basketball_nba":
                if "pace"   in home_pace: pace_ctx["home_pace"]   = home_pace["pace"]
                if "pace"   in away_pace: pace_ctx["away_pace"]   = away_pace["pace"]
                if "pts_pg" in home_pace: pace_ctx["home_pts_pg"] = home_pace["pts_pg"]
                if "pts_pg" in away_pace: pace_ctx["away_pts_pg"] = away_pace["pts_pg"]
            elif league == "icehockey_nhl":
                if "goals_pg" in home_pace: pace_ctx["home_goals_pg"] = home_pace["goals_pg"]
                if "goals_pg" in away_pace: pace_ctx["away_goals_pg"] = away_pace["goals_pg"]
            elif league == "baseball_mlb":
                if "runs_pg" in home_pace: pace_ctx["home_runs_pg"] = home_pace["runs_pg"]
                if "runs_pg" in away_pace: pace_ctx["away_runs_pg"] = away_pace["runs_pg"]
            if pace_ctx:
                ctx["pace"] = pace_ctx

        if ctx:
            ctx["fetched_at"] = datetime.now(timezone.utc).isoformat()

        return ctx

    except Exception as exc:
        log.warning(
            "enrich_game(%s, home=%r away=%r): unexpected error: %s",
            league, home_team, away_team, exc,
        )
        return {}
