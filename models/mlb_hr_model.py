"""
models/mlb_hr_model.py — MLB Home Run probability model.

For each batter_home_runs prop, computes true P(HR in game) using:
  • Batter season HR/PA + recent 15-game form blend
  • Pitcher HR/9 rate vs league average (1.30)
  • Stadium HR park factor (static lookup)
  • Wind speed from stored game_context weather

Public API:
  enrich_hr_props(db)   — score all today's HR props in EVBetCache, commit to DB
  score_hr_prop(bet_dict, pitcher_map) — score a single bet dict
  build_pitcher_map()   — fetch today's pitchers, return team→pitcher dict

Results stored in EVBetCache.hr_model_prob / hr_model_score / hr_model_meta (JSON).
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

CURRENT_SEASON   = datetime.now(timezone.utc).year
LEAGUE_AVG_HR9   = 1.30   # 2024 MLB league average HR/9 allowed
AVG_PA_PER_GAME  = 3.8    # average plate appearances per batter per game

_MLB_BASE = "https://statsapi.mlb.com/api/v1"

# HR park factors — multi-year averages (2023-2025), home team full name → factor
HR_PARK_FACTORS: dict[str, float] = {
    "Colorado Rockies":        1.18,
    "Cincinnati Reds":         1.13,
    "Philadelphia Phillies":   1.07,
    "Texas Rangers":           1.06,
    "Milwaukee Brewers":       1.05,
    "Atlanta Braves":          1.04,
    "Boston Red Sox":          1.03,
    "Chicago Cubs":            1.03,
    "Pittsburgh Pirates":      1.01,
    "New York Yankees":        1.01,
    "Los Angeles Angels":      1.00,
    "Detroit Tigers":          1.00,
    "Baltimore Orioles":       0.99,
    "Kansas City Royals":      0.98,
    "St. Louis Cardinals":     0.98,
    "New York Mets":           0.97,
    "Houston Astros":          0.97,
    "Arizona Diamondbacks":    0.97,
    "Washington Nationals":    0.96,
    "Tampa Bay Rays":          0.96,
    "Chicago White Sox":       0.95,
    "Minnesota Twins":         0.95,
    "San Diego Padres":        0.93,
    "Toronto Blue Jays":       0.94,
    "Los Angeles Dodgers":     0.93,
    "Cleveland Guardians":     0.92,
    "Miami Marlins":           0.91,
    "Oakland Athletics":       0.90,
    "Seattle Mariners":        0.89,
    "San Francisco Giants":    0.84,
}

# Parks where wind doesn't affect HR (indoor/retractable roof typically closed)
_INDOOR_PARKS: set[str] = {
    "Houston Astros", "Tampa Bay Rays", "Minnesota Twins",
    "Seattle Mariners", "Toronto Blue Jays", "Arizona Diamondbacks",
}

# Module-level caches — survive across multiple calls within one pipeline run
_player_info_cache: dict[str, dict] = {}   # player_name → {id, team}
_batter_stat_cache: dict[int, dict]  = {}  # player_id   → stat profile
_pitcher_hr9_cache: dict[int, float] = {}  # pitcher_id  → hr9


# ---------------------------------------------------------------------------
# MLB Stats API helper
# ---------------------------------------------------------------------------

def _mlb_get(path: str, params: dict | None = None) -> dict:
    try:
        r = requests.get(f"{_MLB_BASE}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.debug("HR model MLB API error %s: %s", path, exc)
        return {}


# ---------------------------------------------------------------------------
# Player lookups
# ---------------------------------------------------------------------------

def _get_player_info(player_name: str) -> dict:
    """Return {id, team} for a player by name (MLB Stats API search). Cached."""
    if player_name in _player_info_cache:
        return _player_info_cache[player_name]

    data = _mlb_get("/people/search", {"names": player_name, "sportId": 1})
    people = data.get("people", [])
    if people:
        p = people[0]
        result = {
            "id":   p.get("id"),
            "team": p.get("currentTeam", {}).get("name", ""),
        }
    else:
        result = {"id": None, "team": ""}

    _player_info_cache[player_name] = result
    return result


# ---------------------------------------------------------------------------
# Stat fetchers
# ---------------------------------------------------------------------------

def _get_batter_profile(player_id: int) -> dict:
    """
    Fetch batter HR rate profile. Returns:
      hr_per_pa, pa, recent_hr_per_pa (last 15 games, None if < 30 PA)
    """
    if player_id in _batter_stat_cache:
        return _batter_stat_cache[player_id]

    result = {"hr_per_pa": 0.035, "pa": 0, "recent_hr_per_pa": None}

    # Season totals
    data = _mlb_get(f"/people/{player_id}/stats", {
        "stats": "season", "group": "hitting", "season": CURRENT_SEASON,
    })
    for sg in data.get("stats", []):
        for split in sg.get("splits", []):
            s  = split.get("stat", {})
            pa = int(s.get("plateAppearances", 0) or 0)
            hr = int(s.get("homeRuns", 0) or 0)
            if pa >= 10:
                result["hr_per_pa"] = hr / pa
                result["pa"]        = pa

    # Recent form — last 15 games
    data = _mlb_get(f"/people/{player_id}/stats", {
        "stats": "gameLog", "group": "hitting",
        "season": CURRENT_SEASON, "limit": 15,
    })
    r_pa = r_hr = 0
    for sg in data.get("stats", []):
        for split in sg.get("splits", [])[:15]:
            s     = split.get("stat", {})
            r_pa += int(s.get("plateAppearances", 0) or 0)
            r_hr += int(s.get("homeRuns", 0) or 0)
    if r_pa >= 30:
        result["recent_hr_per_pa"] = r_hr / r_pa

    _batter_stat_cache[player_id] = result
    return result


def _get_pitcher_hr9(pitcher_id: int) -> float:
    """Return pitcher's HR/9 this season. Falls back to league average."""
    if pitcher_id in _pitcher_hr9_cache:
        return _pitcher_hr9_cache[pitcher_id]

    data = _mlb_get(f"/people/{pitcher_id}/stats", {
        "stats": "season", "group": "pitching", "season": CURRENT_SEASON,
    })
    for sg in data.get("stats", []):
        for split in sg.get("splits", []):
            s      = split.get("stat", {})
            ip_str = str(s.get("inningsPitched", "0") or "0")
            try:
                parts = ip_str.split(".")
                ip = float(parts[0]) + (float(parts[1]) / 3 if len(parts) > 1 and parts[1].isdigit() else 0)
            except Exception:
                ip = 0.0
            hr = int(s.get("homeRuns", s.get("homeRunsAllowed", 0)) or 0)
            if ip >= 10:
                hr9 = (hr / ip) * 9.0
                _pitcher_hr9_cache[pitcher_id] = hr9
                return hr9

    _pitcher_hr9_cache[pitcher_id] = LEAGUE_AVG_HR9
    return LEAGUE_AVG_HR9


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _park_info(home_team: str) -> tuple[float, str]:
    """Return (park_factor, display_label) for a home team."""
    f = HR_PARK_FACTORS.get(home_team, 1.0)
    if f >= 1.06:
        return f, f"+{round((f - 1.0) * 100)}% HR park"
    if f <= 0.92:
        return f, f"-{round((1.0 - f) * 100)}% HR park"
    return f, "Neutral park"


def _wind_info(game_context_json: str, home_team: str) -> tuple[float, str]:
    """Parse wind from stored game_context, return (wind_factor, label)."""
    if home_team in _INDOOR_PARKS:
        return 1.0, "Indoor"
    if not game_context_json:
        return 1.0, "Wind N/A"
    try:
        ctx     = json.loads(game_context_json)
        weather = ctx.get("weather", {})
        summary = weather.get("summary", "") if isinstance(weather, dict) else ""
        m = re.search(r"([\d.]+)\s*mph", summary, re.IGNORECASE)
        if m:
            speed = float(m.group(1))
            if speed >= 15:
                return 1.06, f"{speed:.0f}mph (strong)"
            if speed >= 10:
                return 1.03, f"{speed:.0f}mph"
            return 1.0,  f"{speed:.0f}mph (calm)"
    except Exception:
        pass
    return 1.0, "Wind N/A"


# ---------------------------------------------------------------------------
# Probability engine
# ---------------------------------------------------------------------------

def compute_hr_game_prob(
    hr_per_pa:    float,
    expected_pa:  float,
    pitcher_hr9:  float,
    park_factor:  float,
    wind_factor:  float,
    recent_rate:  Optional[float],
) -> float:
    """
    P(≥1 HR in game) via binomial approximation:
        P = 1 − (1 − p_adj)^expected_pa
    where p_adj blends season + recent rate and applies pitcher/park/wind factors.
    """
    # Season/recent blend: 70% season, 30% recent (when recent available)
    base = hr_per_pa
    if recent_rate is not None and recent_rate > 0:
        base = 0.70 * hr_per_pa + 0.30 * recent_rate

    pitcher_factor = min(2.5, max(0.3, pitcher_hr9 / LEAGUE_AVG_HR9))
    p_adj = base * pitcher_factor * park_factor * wind_factor
    p_adj = max(0.005, min(0.30, p_adj))  # per-PA ceiling

    prob = 1.0 - (1.0 - p_adj) ** expected_pa
    return round(max(0.05, min(0.65, prob)), 4)


# ---------------------------------------------------------------------------
# Pitcher map builder
# ---------------------------------------------------------------------------

def build_pitcher_map(pitcher_data: Optional[dict] = None) -> dict:
    """
    Build {batter_team_name → {id, name, hr9}} from probable pitcher data.

    Key insight: batter faces the OPPOSING pitcher.
    Away batters face the Home pitcher; Home batters face the Away pitcher.
    """
    if pitcher_data is None:
        from scripts.context_fetcher import fetch_mlb_probable_pitchers
        pitcher_data = fetch_mlb_probable_pitchers()

    pmap: dict[str, dict] = {}
    for _gk, entry in (pitcher_data or {}).items():
        home_team = entry.get("home_team", "")
        away_team = entry.get("away_team", "")
        home_p    = entry.get("home") or {}
        away_p    = entry.get("away") or {}

        # Away batters face home pitcher
        if away_team and home_p:
            pmap[away_team] = {
                "id":   home_p.get("id"),
                "name": home_p.get("name", "TBA"),
                "hr9":  LEAGUE_AVG_HR9,
            }
        # Home batters face away pitcher
        if home_team and away_p:
            pmap[home_team] = {
                "id":   away_p.get("id"),
                "name": away_p.get("name", "TBA"),
                "hr9":  LEAGUE_AVG_HR9,
            }

    return pmap


# ---------------------------------------------------------------------------
# Single-bet scorer
# ---------------------------------------------------------------------------

def score_hr_prop(bet_dict: dict, pitcher_map: Optional[dict] = None) -> Optional[dict]:
    """
    Score one batter_home_runs prop bet.

    Returns dict with hr_model_prob, hr_model_score, hr_model_meta (JSON str),
    or None if the batter can't be resolved or has < 10 PA.
    """
    player_name = (bet_dict.get("player_name") or "").strip()
    if not player_name:
        return None

    game   = (bet_dict.get("game") or "").strip()
    odds   = int(bet_dict.get("odds") or 300)
    imp_p  = float(bet_dict.get("implied_prob") or 0.0)
    if not imp_p:
        if odds > 0:
            imp_p = 100.0 / (odds + 100.0) / 100.0
        else:
            imp_p = abs(odds) / (abs(odds) + 100.0) / 100.0
    elif imp_p > 1.0:
        imp_p /= 100.0

    gc_json = bet_dict.get("game_context") or ""

    # Park context
    home_team = game.split(" @ ", 1)[1].strip() if " @ " in game else ""
    park_factor, park_label = _park_info(home_team)
    wind_factor, wind_label = _wind_info(gc_json, home_team)

    # Pitcher lookup
    pitcher_id   = None
    pitcher_name = "TBA"
    pitcher_hr9  = LEAGUE_AVG_HR9

    pinfo = _get_player_info(player_name)
    player_id   = pinfo.get("id")
    batter_team = pinfo.get("team", "")

    if pitcher_map and batter_team:
        pm = pitcher_map.get(batter_team, {})
        pitcher_id   = pm.get("id")
        pitcher_name = pm.get("name", "TBA")
        pitcher_hr9  = pm.get("hr9", LEAGUE_AVG_HR9)

    # Fetch pitcher HR9 lazily
    if pitcher_id and pitcher_hr9 == LEAGUE_AVG_HR9:
        pitcher_hr9 = _get_pitcher_hr9(pitcher_id)
        if pitcher_map and batter_team:
            pitcher_map[batter_team]["hr9"] = pitcher_hr9

    if not player_id:
        return None

    profile = _get_batter_profile(player_id)
    if profile["pa"] < 10:
        return None

    hr_prob = compute_hr_game_prob(
        hr_per_pa   = profile["hr_per_pa"],
        expected_pa = AVG_PA_PER_GAME,
        pitcher_hr9 = pitcher_hr9,
        park_factor = park_factor,
        wind_factor = wind_factor,
        recent_rate = profile.get("recent_hr_per_pa"),
    )

    ev_pct  = float(bet_dict.get("ev_percent") or 0.0)
    edge_pp = (hr_prob - imp_p) * 100.0

    # Composite ranking score — edge is the dominant term
    score = (
        edge_pp * 3.0
        + min(ev_pct, 15.0) * 0.8
        + (pitcher_hr9 - LEAGUE_AVG_HR9) * 5.0
        + (park_factor  - 1.0)            * 20.0
        + (wind_factor  - 1.0)            * 8.0
    )
    if profile["pa"] >= 150:
        score += 2.0
    elif profile["pa"] < 60:
        score -= 2.0

    meta = {
        "pitcher_name":  pitcher_name,
        "pitcher_hr9":   round(pitcher_hr9, 2),
        "park_factor":   round(park_factor, 3),
        "park_label":    park_label,
        "wind_label":    wind_label,
        "batter_hr_ppa": round(profile["hr_per_pa"], 4),
        "batter_pa":     profile["pa"],
    }

    return {
        "hr_model_prob":  hr_prob,
        "hr_model_score": round(score, 2),
        "hr_model_meta":  json.dumps(meta),
        "edge_pp":        round(edge_pp, 2),
    }


# ---------------------------------------------------------------------------
# AI analysis generator
# ---------------------------------------------------------------------------

def _generate_hr_analysis(
    player_name: str,
    game: str,
    odds: int,
    imp_pct: float,
    mod_pct: float,
    edge_pp: float,
    pitcher_name: str,
    pitcher_hr9: float,
    park_label: str,
    wind_label: str,
    batter_hr_ppa: float,
    batter_pa: int,
) -> Optional[str]:
    """
    Call Gemini to generate a concise 2-3 sentence betting rationale for an HR prop.
    Returns the analysis string or None on failure.
    """
    import os
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    hr_ppa_pct = round(batter_hr_ppa * 100, 2)
    odds_str   = f"+{odds}" if odds > 0 else str(odds)

    if pitcher_name and pitcher_name != "TBA":
        pct_vs_avg = round(abs(pitcher_hr9 / LEAGUE_AVG_HR9 - 1.0) * 100)
        direction  = "above" if pitcher_hr9 > LEAGUE_AVG_HR9 else "below"
        pitcher_ctx = (
            f"{pitcher_name} is starting — {pitcher_hr9} HR/9 "
            f"({pct_vs_avg}% {direction} league avg of {LEAGUE_AVG_HR9})"
        )
    else:
        pitcher_ctx = f"Starting pitcher TBA — using league-average HR/9 ({LEAGUE_AVG_HR9})"

    wind_ctx = "" if wind_label in ("Wind N/A", "Indoor", "") else f"\n- Wind: {wind_label}"

    system_prompt = (
        "You are a sharp MLB betting analyst. Write a confident, concise 2-3 sentence "
        "rationale for a home run prop bet. Be specific about the key factors driving the "
        "edge — the pitcher matchup, park, and batter's rate. No bullet points, no headers, "
        "no em-dashes to open sentences. Write in direct prose. Don't start with the player's name."
    )

    user_prompt = (
        f"HR Prop: {player_name} to hit a home run\n"
        f"Game: {game}\n"
        f"Odds: {odds_str} — book-implied {imp_pct:.1f}%, model {mod_pct:.1f}% (+{edge_pp:.1f}pp edge)\n\n"
        f"Context:\n"
        f"- {pitcher_ctx}\n"
        f"- Park: {park_label}"
        f"{wind_ctx}\n"
        f"- {player_name}: {hr_ppa_pct}% HR/PA this season ({batter_pa} PA)\n\n"
        f"Write 2-3 sentences explaining why this bet has value. Be direct and specific."
    )

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=system_prompt,
        )
        response = model.generate_content(
            user_prompt,
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=200, temperature=0.45
            ),
        )
        text = (response.text or "").strip()
        if text:
            log.info("_generate_hr_analysis: wrote analysis for %s", player_name)
            return text
        log.warning("_generate_hr_analysis: empty response for %s", player_name)
    except Exception as exc:
        log.warning("_generate_hr_analysis: failed for %s: %s", player_name, exc)

    return None


# ---------------------------------------------------------------------------
# Bulk enrichment (called from pipeline)
# ---------------------------------------------------------------------------

def enrich_hr_props(db) -> int:
    """
    Score all upcoming batter_home_runs props in EVBetCache.
    Clears module caches first so stale data from previous runs doesn't persist.
    Returns the number of rows scored and committed.
    """
    from db.database import EVBetCache  # lazy import to avoid circular deps

    now_utc = datetime.now(timezone.utc)

    rows = (
        db.query(EVBetCache)
        .filter(
            EVBetCache.market == "batter_home_runs",
            EVBetCache.is_prop == True,  # noqa: E712
            (EVBetCache.commence_time == None) |  # noqa: E711
            (EVBetCache.commence_time > now_utc),
        )
        .all()
    )

    if not rows:
        log.debug("enrich_hr_props: no HR props to score")
        return 0

    log.info("enrich_hr_props: scoring %d HR props", len(rows))

    # Flush per-run caches
    _player_info_cache.clear()
    _batter_stat_cache.clear()
    _pitcher_hr9_cache.clear()

    try:
        pitcher_map = build_pitcher_map()
    except Exception as exc:
        log.warning("enrich_hr_props: pitcher map failed: %s", exc)
        pitcher_map = {}

    scored = 0
    for row in rows:
        try:
            result = score_hr_prop(
                {
                    "player_name":  row.player_name or "",
                    "game":         row.game or "",
                    "odds":         row.odds or 300,
                    "implied_prob": row.implied_prob or 0.0,
                    "ev_percent":   row.ev_percent or 0.0,
                    "game_context": row.game_context or "",
                },
                pitcher_map,
            )
            if result is None:
                continue

            meta_dict = json.loads(result["hr_model_meta"])

            # Preserve cached analysis so we don't burn Gemini credits every run
            existing_analysis = None
            if row.hr_model_meta:
                try:
                    existing_analysis = json.loads(row.hr_model_meta).get("analysis")
                except Exception:
                    pass

            if existing_analysis:
                meta_dict["analysis"] = existing_analysis
            else:
                imp = float(row.implied_prob or 0)
                if imp > 1.0:
                    imp /= 100.0
                analysis = _generate_hr_analysis(
                    player_name  = row.player_name or "",
                    game         = row.game or "",
                    odds         = int(row.odds or 300),
                    imp_pct      = imp * 100,
                    mod_pct      = result["hr_model_prob"] * 100,
                    edge_pp      = result["edge_pp"],
                    pitcher_name = meta_dict.get("pitcher_name", "TBA"),
                    pitcher_hr9  = meta_dict.get("pitcher_hr9", LEAGUE_AVG_HR9),
                    park_label   = meta_dict.get("park_label", "Neutral park"),
                    wind_label   = meta_dict.get("wind_label", "Wind N/A"),
                    batter_hr_ppa = meta_dict.get("batter_hr_ppa", 0.035),
                    batter_pa    = meta_dict.get("batter_pa", 0),
                )
                if analysis:
                    meta_dict["analysis"] = analysis

            row.hr_model_prob  = result["hr_model_prob"]
            row.hr_model_score = result["hr_model_score"]
            row.hr_model_meta  = json.dumps(meta_dict)
            scored += 1
        except Exception as exc:
            log.warning("enrich_hr_props: failed %s: %s", row.player_name, exc)

    try:
        db.commit()
        log.info("enrich_hr_props: committed %d/%d scored props", scored, len(rows))
    except Exception as exc:
        log.error("enrich_hr_props: commit failed: %s", exc)
        db.rollback()

    return scored
