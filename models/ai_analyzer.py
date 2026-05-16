"""
models/ai_analyzer.py — AI-powered bet analysis using Claude + Optimal data.

For each +EV bet, this module:
1. Fetches live context from the Optimal Bet MCP server (projections, team
   history, recent form, market consensus).
2. Calls Claude claude-opus-4-6 with adaptive thinking to generate a structured
   analysis including:
   - Improved true probability estimate
   - Confidence score (1–100)
   - Kelly criterion sizing (¼ Kelly / 25% fractional)
   - Natural language "Why This Pick Makes Sense" with:
       (A) Mathematical Justification
       (B) Real-World Contextual Validation

Usage
-----
    from models.ai_analyzer import analyze_bet

    bet = {
        "id": 42,
        "game": "Orlando Magic @ Dallas Mavericks",
        "market": "h2h",
        "team": "Orlando Magic",
        "odds": 140,
        "true_prob": 0.38,
        "ev_percent": 5.2,
        "league": "basketball_nba",
        "point": None,
        "player_name": None,
        "is_prop": False,
    }
    result = analyze_bet(bet)
    # result.keys(): analysis, confidence_score, kelly_pct, true_prob_refined
"""

import json
import logging
import math
import os
import sys
from typing import Any, Optional

import anthropic

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.optimal_client import OptimalClient
from scripts.context_fetcher import (
    fetch_mlb_probable_pitchers, _match_mlb_game, fetch_game_projections,
    fetch_game_context, fetch_pitcher_vs_team_stats,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL = "claude-opus-4-6"

# League → Optimal league key mapping
_LEAGUE_MAP = {
    "basketball_nba":          "nba",
    "baseball_mlb":            "mlb",
    "icehockey_nhl":           "nhl",

    # Soccer league codes — Optimal MCP uses lowercase abbreviations
    "soccer_epl":                "epl",
    "soccer_spain_la_liga":      "laliga",
    "soccer_germany_bundesliga": "bundesliga",
    "soccer_usa_mls":            "mls",
    "soccer_uefa_champs_league": "ucl",
}

# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def _american_to_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def _american_to_decimal(odds: int) -> float:
    if odds > 0:
        return (odds / 100) + 1.0
    return (100 / abs(odds)) + 1.0


def _kelly(true_prob: float, odds: int, fraction: float = 0.25) -> float:
    """Fractional Kelly criterion. Returns % of bankroll to wager."""
    decimal = _american_to_decimal(odds)
    b = decimal - 1  # net odds
    q = 1 - true_prob
    k = (b * true_prob - q) / b
    return max(0.0, round(k * fraction * 100, 2))


def _ev_pct(true_prob: float, odds: int) -> float:
    decimal = _american_to_decimal(odds)
    profit_if_win = decimal - 1
    ev = true_prob * profit_if_win - (1 - true_prob)
    return round(ev * 100, 2)


# ---------------------------------------------------------------------------
# Context builder — fetch live data from Optimal
# ---------------------------------------------------------------------------

def _fetch_team_form(team_name: str, league_key: str, client: OptimalClient) -> Optional[Any]:
    """
    Fetch recent form for a team using two strategies:
    1. search_teams → name match → get_team_history (preferred when team is in results)
    2. SQL teams lookup → get_team_history (handles alphabetically-late teams that
       search_teams truncates, e.g. VGK/WSH/WPG in NHL whose list stops ~24 entries;
       also replaces the old freeform query fallback since Optimal now requires SQL)
    Returns the form data or None if both fail.
    """
    # ── Strategy 1: search → name-match → get_team_history ───────────────
    try:
        teams = client.search_teams(team_name, league=league_key) or []
        if isinstance(teams, list) and teams:
            search_words = [w for w in team_name.lower().split() if len(w) > 2]
            matched_team = None
            for t in teams:
                dn = t.get("display_name", "").lower()
                tk = t.get("team_key", "").lower()
                if any(w in dn for w in search_words) or tk in team_name.lower():
                    matched_team = t
                    break

            if matched_team is None:
                log.warning("Optimal context: search_teams found no name match for '%s' among %d results",
                            team_name, len(teams))
            else:
                team_id = (
                    matched_team.get("id")
                    or matched_team.get("team_id")
                    or matched_team.get("teamId")
                    or matched_team.get("team_key")
                )
                if team_id:
                    hist = client.get_team_history(str(team_id), last_n=10)
                    if hist:
                        log.info("Optimal context: team history fetched via search for %s (id=%s)", team_name, team_id)
                        return hist
                    log.warning("Optimal context: get_team_history returned nothing for %s (id=%s)", team_name, team_id)
                else:
                    log.warning("Optimal context: matched team has no usable id for %s. Keys: %s",
                                team_name, list(matched_team.keys()))
        else:
            log.warning("Optimal context: search_teams returned no results for %s in %s", team_name, league_key)
    except Exception as exc:
        log.warning("Optimal context: search/history chain failed for %s: %s", team_name, exc)

    # ── Strategy 2: SQL teams lookup → get_team_history ──────────────────
    # search_teams returns teams alphabetically and truncates at ~24 entries,
    # cutting off teams like VGK (V), WSH, WPG. Querying the teams table
    # directly by display_name LIKE pattern resolves the UUID regardless of
    # alphabetical position.  Optimal requires valid SQL — freeform text is rejected.
    try:
        # Use the longest meaningful words (>3 chars) for LIKE matching
        key_words = sorted(
            [w for w in team_name.lower().split() if len(w) > 3],
            key=len, reverse=True
        )[:2]
        if key_words:
            like_clauses = " OR ".join(
                f"LOWER(display_name) LIKE '%{w}%'" for w in key_words
            )
            # Include league filter so WNBA/NBA teams don't match NHL queries etc.
            sql = (
                f"SELECT id, team_key, display_name, league FROM teams "
                f"WHERE league = '{league_key}' AND ({like_clauses}) LIMIT 5"
            )
            team_rows = client.query(sql)
            if team_rows and isinstance(team_rows, list):
                # Pick the row whose display_name contains the most search words
                search_words = [w for w in team_name.lower().split() if len(w) > 2]
                best_row, best_hits = None, 0
                for row in team_rows:
                    dn = row.get("display_name", "").lower()
                    hits = sum(1 for w in search_words if w in dn)
                    if hits > best_hits:
                        best_hits, best_row = hits, row
                if best_row and best_hits > 0:
                    team_id = best_row.get("id") or best_row.get("team_id") or best_row.get("teamId")
                    if team_id:
                        hist = client.get_team_history(str(team_id), last_n=10)
                        if hist:
                            log.info("Optimal context: team history via SQL teams lookup for %s (id=%s)",
                                     team_name, team_id)
                            return hist
            log.warning("Optimal context: SQL teams lookup returned nothing for %s", team_name)
    except Exception as exc:
        log.warning("Optimal context: SQL teams lookup failed for %s: %s", team_name, exc)

    return None


def _build_context(bet: dict, client: OptimalClient) -> dict:
    """
    Fetch relevant live data from the Optimal MCP server for the given bet.
    Returns a dict of context sections to pass to Claude.
    """
    ctx: dict = {}
    league_key = _LEAGUE_MAP.get(bet.get("league", ""), "nba")
    game_str = bet.get("game", "")
    is_prop = bet.get("is_prop", False)
    player_name = bet.get("player_name")
    market = bet.get("market", "h2h")

    # Derive game_date (YYYYMMDD in ET) from commence_time so get_events
    # targets the correct calendar day — essential for tomorrow's bets.
    # NOTE: Optimal get_events expects compact YYYYMMDD format, NOT YYYY-MM-DD.
    game_date: Optional[str] = None
    ct = bet.get("commence_time")
    if ct is not None:
        try:
            from zoneinfo import ZoneInfo
            if hasattr(ct, "astimezone"):
                game_date = ct.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        except Exception:
            pass

    # ── 1. Upcoming events to find game_id ───────────────────────────────
    try:
        events = client.get_events(league_key, date=game_date) or []
        if isinstance(events, list) and events:
            # Try to locate the specific game from the event list
            home, away = "", ""
            if " @ " in game_str:
                away, home = game_str.split(" @ ", 1)
            for ev in events:
                ev_str = str(ev).lower()
                if (home.lower() in ev_str or away.lower() in ev_str):
                    ctx["game_event"] = ev
                    break
            if not ctx.get("game_event"):
                log.warning("Optimal context: could not locate game '%s' in %d events for %s",
                            game_str, len(events), league_key)
        else:
            log.warning("Optimal context: get_events returned empty for league=%s", league_key)
    except Exception as exc:
        log.warning("Optimal context: events fetch failed for %s: %s", league_key, exc)

    # ── 2. Recent form for both teams ────────────────────────────────────
    # Primary path: use home_team_id / away_team_id already embedded in the
    # game event — avoids search_teams, which returns all teams unfiltered.
    # Fallback: search_teams with name matching for when no event was found.
    if " @ " in game_str:
        away_team, home_team = [s.strip() for s in game_str.split(" @ ", 1)]
        game_event = ctx.get("game_event") or {}

        away_tid = game_event.get("away_team_id")
        home_tid = game_event.get("home_team_id")

        for team_name, team_id in [(away_team, away_tid), (home_team, home_tid)]:
            hist = None
            if team_id:
                try:
                    hist = client.get_team_history(team_id, last_n=10)
                    if hist:
                        log.info("Optimal context: team history via event ID for %s (%s)", team_name, team_id)
                    else:
                        log.warning("Optimal context: get_team_history empty for %s (id=%s)", team_name, team_id)
                except Exception as exc:
                    log.warning("Optimal context: get_team_history failed for %s: %s", team_name, exc)

            if not hist:
                # Fallback: search_teams and match by display_name
                hist = _fetch_team_form(team_name, league_key, client)

            if hist is not None:
                ctx.setdefault("team_history", {})[team_name] = hist

    if not ctx.get("team_history"):
        log.warning("Optimal context: no team history captured for game='%s'", game_str)

    # ── 3. Player context (props only) ────────────────────────────────────
    if is_prop and player_name:
        try:
            players = client.search_players(player_name, league=league_key) or []
            player_id = None
            if isinstance(players, list) and players:
                # search_players returns all players unfiltered — find best name match
                search_words = [w for w in player_name.lower().split() if len(w) > 1]
                for p in players:
                    fn = p.get("full_name", p.get("display_name", p.get("name", ""))).lower()
                    if all(w in fn for w in search_words):
                        player_id = p.get("id") or p.get("player_id") or p.get("playerId")
                        log.info("Optimal context: matched player '%s' → id=%s", player_name, player_id)
                        break

                if player_id:
                    gamelogs = client.get_player_gamelogs(player_id, last_n=10)
                    if gamelogs:
                        ctx["player_gamelogs"] = gamelogs
                    else:
                        log.warning("Optimal context: player gamelogs empty for %s (id=%s)", player_name, player_id)

                    game_event = ctx.get("game_event") or {}
                    game_id = game_event.get("id") or game_event.get("game_id")
                    if game_id:
                        proj = client.get_player_projections(player_id, game_id=game_id)
                        if proj:
                            ctx["player_projections"] = proj
                else:
                    log.warning("Optimal context: no name match for player '%s' among %d results",
                                player_name, len(players))
            else:
                log.warning("Optimal context: search_players returned nothing for %s", player_name)
        except Exception as exc:
            log.warning("Optimal context: player data failed for %s: %s", player_name, exc)

    # ── 4. Market consensus odds ──────────────────────────────────────────
    try:
        game_event = ctx.get("game_event", {})
        game_id = game_event.get("game_id") or game_event.get("id") if game_event else None
        if game_id:
            odds_data = client.get_game_odds(game_id)
            if odds_data:
                ctx["market_odds"] = odds_data
            else:
                log.warning("Optimal context: get_game_odds returned nothing for game_id=%s", game_id)
        else:
            log.warning("Optimal context: no game_id available, skipping market odds fetch")
    except Exception as exc:
        log.warning("Optimal context: market odds failed: %s", exc)

    # ── 5. MLB pitcher matchup (MLB only) ─────────────────────────────────
    if bet.get("league") == "baseball_mlb" and " @ " in game_str:
        away_team, home_team = game_str.split(" @ ", 1)
        away_team, home_team = away_team.strip(), home_team.strip()

        # Primary: MLB Stats API (free, reliable, no rate limits)
        try:
            mlb_pitchers = fetch_mlb_probable_pitchers()
            matched = _match_mlb_game(mlb_pitchers, away_team, home_team)
            if matched:
                ctx["pitcher_matchup"] = matched
                log.info("context: MLB Stats API pitcher data found for %s @ %s", away_team, home_team)
            else:
                log.warning("context: MLB Stats API returned no match for %s @ %s (%d games fetched)",
                            away_team, home_team, len(mlb_pitchers))
        except Exception as exc:
            log.warning("context: MLB Stats API pitcher fetch failed for %s: %s", game_str, exc)

        # Fallback: Optimal query() if Stats API returned nothing
        if not ctx.get("pitcher_matchup"):
            try:
                _date_phrase = f"on {game_date}" if game_date else "today"
                q = (
                    f"Who are the confirmed starting pitchers for the {away_team} vs {home_team} "
                    f"MLB game {_date_phrase}? Include each pitcher's name, current ERA, WHIP, and their "
                    f"last 3 outings with IP, ER, and strikeouts."
                )
                pitcher_data = client.query(q)
                if pitcher_data:
                    ctx["pitcher_matchup"] = {"optimal_query": pitcher_data}
                    log.info("context: pitcher matchup fetched via Optimal fallback for %s", game_str)
                else:
                    log.warning("context: Optimal pitcher query also returned nothing for %s", game_str)
            except Exception as exc:
                log.warning("context: Optimal pitcher fallback failed for %s: %s", game_str, exc)

    # ── 6. Game projections (all non-prop game bets) ──────────────────────
    if not is_prop and " @ " in game_str:
        league_code = bet.get("league", "")
        proj = fetch_game_projections(game_str, league_code)
        if proj:
            ctx["game_projections"] = proj
            log.info(
                "context: game projections loaded for %s — spread=%.2f total=%.2f",
                game_str,
                proj.get("spread_mean", 0),
                proj.get("total_mean", 0),
            )
        else:
            log.debug("context: no game projections returned for %s (%s)", game_str, league_code)

    # ── 7. Situational context: records, streaks, playoff position, series notes ──
    # fetch_game_context hits ESPN scoreboard + NHL standings / NBA standings
    # and returns: home/away_record, home/away_streak, home/away_last10,
    # home/away_pts (NHL), home/away_conf_rank (NHL), home/away_playoff_note,
    # home/away_playoff_seed (NBA), game_notes.
    if not is_prop and " @ " in game_str:
        try:
            gc = fetch_game_context(
                game_str,
                bet.get("league", ""),
                commence_dt=bet.get("commence_time"),
            )
            if gc:
                # Drop raw team name keys (already known); keep all signal fields
                ctx["game_situation"] = {
                    k: v for k, v in gc.items()
                    if k not in ("away_team", "home_team")
                }
                log.info("context: game_situation loaded for %s — keys: %s",
                         game_str, list(ctx["game_situation"].keys()))
            else:
                log.debug("context: fetch_game_context returned empty for %s", game_str)
        except Exception as exc:
            log.debug("context: fetch_game_context failed for %s: %s", game_str, exc)

    # ── 8. Pitcher-vs-opponent historical stats (MLB only) ───────────────
    # Uses the same pitcher_matchup data already fetched in section 5.
    # Enriches each pitcher entry with their stats against this specific team.
    if bet.get("league") == "baseball_mlb" and " @ " in game_str:
        pm = ctx.get("pitcher_matchup")
        if isinstance(pm, dict) and not pm.get("optimal_query"):
            away_name, home_name = [s.strip() for s in game_str.split(" @ ", 1)]
            try:
                # Fetch opposing team IDs from the MLB schedule (embedded in pitcher_matchup)
                away_team_id = pm.get("away", {}).get("team_id") or pm.get("away_team_id")
                home_team_id = pm.get("home", {}).get("team_id") or pm.get("home_team_id")

                # Away pitcher vs home team
                away_p = pm.get("away", {})
                away_pitcher_id = away_p.get("pitcher_id") or away_p.get("id")
                if away_pitcher_id and home_team_id:
                    vs_stats = fetch_pitcher_vs_team_stats(int(away_pitcher_id), int(home_team_id))
                    if vs_stats and int(vs_stats.get("games", 0)) > 0:
                        ctx.setdefault("pitcher_vs_team", {})["away_pitcher"] = {
                            "name": away_p.get("name", "Away starter"),
                            "vs_team": away_name.split()[-1] + " (home)",
                            **vs_stats,
                        }

                # Home pitcher vs away team
                home_p = pm.get("home", {})
                home_pitcher_id = home_p.get("pitcher_id") or home_p.get("id")
                if home_pitcher_id and away_team_id:
                    vs_stats = fetch_pitcher_vs_team_stats(int(home_pitcher_id), int(away_team_id))
                    if vs_stats and int(vs_stats.get("games", 0)) > 0:
                        ctx.setdefault("pitcher_vs_team", {})["home_pitcher"] = {
                            "name": home_p.get("name", "Home starter"),
                            "vs_team": home_name.split()[-1] + " (away)",
                            **vs_stats,
                        }

                if ctx.get("pitcher_vs_team"):
                    log.info("context: pitcher_vs_team data loaded for %s", game_str)
            except Exception as exc:
                log.debug("context: pitcher_vs_team fetch failed for %s: %s", game_str, exc)

    return ctx


# ---------------------------------------------------------------------------
# Sport-specific analysis requirements
# ---------------------------------------------------------------------------

_SPORT_CONTEXT = {
    "basketball_nba": """
Lead with the single strongest fact for this bet (use only what's in context):
- INJURIES: Check `injuries.home` and `injuries.away`. If a star (All-Star level or primary scorer/playmaker) is Out or Doubtful, name them and state the impact — this is often the lead.
- BACK-TO-BACK: Check `rest.home_b2b` and `rest.away_b2b`. Name the team explicitly if true. B2B teams cover at ~3-4% lower rate — quantify the disadvantage.
- PLAYOFF SEEDING: If either team is within 2 games of a seed boundary or home-court advantage, open with that scenario and both seeds.
- PACE MISMATCH: Check `pace.home_pace` vs `pace.away_pace`. A 5+ possession gap between teams creates over/under edge — state the numbers.
- SHARP STEAM + FORM: If sharp score ≥50 and line moved toward this side, lead with that. Cite the last-10 record and streak.
- PROPS: State the player's actual hit rate vs. this line from gamelogs (e.g. "Over in 7 of last 10 = 70% vs. 54% implied"). This is what makes or breaks the prop case.
Skip anything not supported by the data provided.
""",

    "icehockey_nhl": """
Lead with the single strongest fact for this bet (use only what's in context):
- INJURIES: Check `injuries.home` and `injuries.away`. A top-6 forward or top-4 defenseman Out/Doubtful has direct line impact — name them.
- BACK-TO-BACK: Check `rest.home_b2b` and `rest.away_b2b`. NHL B2B is a well-documented fatigue signal — name the team if true.
- PLAYOFF STAKES: Conference rank, points, gap to wild card or elimination — if meaningful, this is the lead. State exactly what tonight determines.
- GOALTENDER MATCHUP: Name both confirmed starters and state GAA / save%. If unconfirmed, say so.
- FORM: Last-10 record and current streak for each team.
Skip anything not in the data.
""",

    "baseball_mlb": """
Lead with the single strongest fact for this bet (use only what's in context):
- PITCHER VS. THIS TEAM (highest priority if pitcher_vs_team is present): State the starter's record, ERA, and number of starts against this specific opponent. This is more predictive than season ERA.
- PITCHER SEASON STATS: ERA, WHIP. Mention the last start result if available.
- WEATHER: Check `weather`. Wind >15 mph to center suppresses home runs and scoring; rain affects totals. State speed and direction if relevant.
- INJURIES: Check `injuries.home` and `injuries.away`. A lineup's cleanup hitter or ace out changes the game total picture.
- SERIES CONTEXT: Sweep scenario or series lead — strong motivational signal.
- TEAM FORM: Current streak and last-10 record.
Skip anything not in the data.
CRITICAL — pitcher names: Only cite a starter by name if their name appears in `pitcher_matchup`, `home_pitcher`, or `away_pitcher` in the context data. Never assert a pitcher name from training knowledge — starters get scratched, traded, and reassigned constantly and your training data is months stale. If the context shows no pitcher data, write "starter TBA" rather than guessing.
""",

    "soccer_epl": """
Lead with the single strongest fact: table stakes (relegation, title race, top-4 — with specific points gap) or form. State last 5 W/D/L.
- INJURIES: Check `injuries.home` and `injuries.away`. Named absences from context only — a striker or key midfielder Out changes the goal total picture.
If "home_manager" or "away_manager" is in the context data, you may reference the manager by that name. If those fields are absent, do NOT mention managers at all.
""",

    "soccer_spain_la_liga": """
Lead with La Liga table stakes (title, UCL fight, relegation) with specific points gap if applicable. State last 5 form.
- INJURIES: Check `injuries.home` and `injuries.away`. Named absences from context only.
If "home_manager" or "away_manager" is in the context data, you may reference the manager by that name. If those fields are absent, do NOT mention managers at all.
""",

    "soccer_germany_bundesliga": """
Lead with Bundesliga table stakes if applicable. State last 5 form and current streak.
- INJURIES: Check `injuries.home` and `injuries.away`. Named absences from context only.
If "home_manager" or "away_manager" is in the context data, you may reference the manager by that name. If those fields are absent, do NOT mention managers at all.
""",

    "soccer_usa_mls": """
State last 5 form and current streak. Mention playoff positioning if within 3 points. MLS home advantage is meaningful — note home/away record split if in the data.
- INJURIES: Check `injuries.home` and `injuries.away`. Named absences from context only.
If "home_manager" or "away_manager" is in the context data, you may reference the manager by that name. If those fields are absent, do NOT mention managers at all.
""",

    "soccer_uefa_champs_league": """
If knockout second leg: state the aggregate score and exactly what each team needs to advance — this is the lead, and the most important fact. Suspension risk (one yellow from a ban). Last 5 form.
- INJURIES: Check `injuries.home` and `injuries.away`. A key striker or holding mid Out is major context.
If "home_manager" or "away_manager" is in the context data, you may reference the manager by that name. If those fields are absent, do NOT mention managers at all.
""",
}

_SYSTEM_PROMPT = """You are a sharp, experienced sports bettor giving a friend a quick rundown on why a specific pick has real edge. Write like a knowledgeable handicapper — confident, specific, and direct. Not a research report.

Tone rules (strictly enforced):
- No section headers, no labels like "Mathematical Edge" or "Market & Situational Context." Just clear, flowing sentences.
- Lead with the single strongest real-world fact you have. Don't bury the lede. If sharp money has steamed this line, say it first. If a pitcher is 0-3 with a 6.75 ERA against this lineup, say that first.
- Weave the market angle in naturally — where the probability gap sits, why the book is wrong — without reciting formulas or using terms like "no-vig" or "implied probability."
- Numbers anchor every claim. "Boston is 8-2 over their last 10" beats "Boston has been playing well." "Line moved from +130 to +115 overnight" beats "the line steamed."
- If only pipeline signals are available (no live context), make those compelling — sharp money %, line movement direction, model edge. Do NOT say "insufficient live data" or list what data you wish you had.
- 1 sentence maximum on risk. Name something specific that could sink this bet.
- Never reference internal systems, field names, arrays, or API terminology.
- CRITICAL — coaching staff and rosters: Never assert a specific manager, head coach, or recent signing/transfer by name unless that name appears explicitly in the Live Context Data provided. Coaching changes and transfers happen constantly; your training data is stale. If the context supplies a manager name (e.g. "home_manager": "Ruben Amorim"), use it. If it does not, omit any reference to coaching staff entirely rather than risk citing someone who was fired months ago.
- Respond with ONLY the JSON object. No preamble, no markdown fences."""


# ---------------------------------------------------------------------------
# Context quality checker
# ---------------------------------------------------------------------------

def _assess_context_quality(ctx: dict) -> tuple[str, int, list]:
    """
    Returns a (quality_label, data_points_count) tuple.
    Used to inform Claude how much real data it actually has.
    """
    points = 0
    fields = []

    if ctx.get("team_history"):
        th = ctx["team_history"]
        teams_with_data = [t for t, v in th.items() if v]
        if teams_with_data:
            points += len(teams_with_data) * 3
            fields.append(f"team history for {', '.join(teams_with_data)}")

    if ctx.get("game_event"):
        points += 2
        fields.append("game event details")

    if ctx.get("market_odds"):
        points += 3
        fields.append("multi-book market odds")

    if ctx.get("player_gamelogs"):
        points += 4
        fields.append("player gamelogs")

    if ctx.get("player_projections"):
        points += 3
        fields.append("player projections")

    if ctx.get("game_projections"):
        gp = ctx["game_projections"]
        detail = []
        if gp.get("spread_mean") is not None:
            detail.append(f"spread {gp['spread_mean']:+.1f}")
        if gp.get("total_mean") is not None:
            detail.append(f"total {gp['total_mean']:.1f}")
        if gp.get("home_win_probability") is not None:
            detail.append(f"home win {gp['home_win_probability']*100:.0f}%")
        points += 4
        fields.append(f"game projections ({', '.join(detail)})")

    if ctx.get("pitcher_matchup"):
        pm = ctx["pitcher_matchup"]
        # Structured dict from MLB Stats API: check if at least one pitcher confirmed
        if isinstance(pm, dict) and not pm.get("optimal_query"):
            home_p = pm.get("home") or {}
            away_p = pm.get("away") or {}
            confirmed = [p.get("name") for p in [home_p, away_p] if p and p.get("name")]
            if confirmed:
                points += 4
                fields.append(f"confirmed pitchers: {', '.join(confirmed)}")
            else:
                points += 1
                fields.append("pitcher matchup (both unconfirmed)")
        else:
            # Raw Optimal query fallback — less structured but still useful
            points += 2
            fields.append("pitcher matchup (Optimal query)")

    if ctx.get("pitcher_stats"):
        points += 3
        fields.append("individual pitcher stats")

    if ctx.get("game_situation"):
        gs = ctx["game_situation"]
        gs_detail = []
        for side in ("home", "away"):
            if gs.get(f"{side}_last10"):
                gs_detail.append(f"{side} last10: {gs[f'{side}_last10']}")
            if gs.get(f"{side}_streak"):
                gs_detail.append(f"{side} streak: {gs[f'{side}_streak']}")
            if gs.get(f"{side}_playoff_note"):
                gs_detail.append(f"{side} playoff: {gs[f'{side}_playoff_note']}")
            if gs.get(f"{side}_conf_rank"):
                gs_detail.append(f"{side} conf#{gs[f'{side}_conf_rank']}")
            if gs.get(f"{side}_playoff_seed"):
                gs_detail.append(f"{side} seed#{gs[f'{side}_playoff_seed']}")
        if gs_detail:
            points += 4
            fields.append(f"game situation ({'; '.join(gs_detail[:4])})")
        else:
            points += 1
            fields.append("game situation (basic)")

    if ctx.get("pitcher_vs_team"):
        pvt = ctx["pitcher_vs_team"]
        parts = []
        for side in ("away_pitcher", "home_pitcher"):
            p = pvt.get(side, {})
            if p and int(p.get("games", 0)) > 0:
                parts.append(f"{p.get('name','?')} vs {p.get('vs_team','opp')}: "
                              f"{p['games']}G ERA {p.get('era','?')}")
        if parts:
            points += 5
            fields.append(f"pitcher vs opponent history ({'; '.join(parts)})")

    # MLB Stats API: count ERA/WHIP data embedded in pitcher_matchup
    pm = ctx.get("pitcher_matchup")
    if isinstance(pm, dict) and not pm.get("optimal_query"):
        stats_count = sum(
            1 for side in ("home", "away")
            if pm.get(side) and pm[side].get("era") is not None
        )
        if stats_count > 0 and "individual pitcher stats" not in " ".join(fields):
            points += stats_count * 1  # +1 per pitcher with ERA/WHIP
            fields.append(f"season stats for {stats_count} pitcher(s)")

    # Stored enrichment keys (injuries, rest, weather, pace)
    inj = ctx.get("injuries", {})
    if isinstance(inj, dict) and (inj.get("home") or inj.get("away")):
        out_list = []
        for side in ("home", "away"):
            for p in inj.get(side, []):
                if isinstance(p, dict) and p.get("status", "").lower() in ("out", "doubtful"):
                    out_list.append(f"{p.get('name','?')} ({side}, {p.get('status','')})")
        if out_list:
            points += 4
            fields.append(f"injuries: {', '.join(out_list[:4])}")
        else:
            points += 1
            fields.append("injury report (no key absences)")

    rest = ctx.get("rest", {})
    if isinstance(rest, dict) and (rest.get("home_rest") is not None or rest.get("away_rest") is not None):
        notes = []
        if rest.get("home_b2b"):
            notes.append("home B2B")
        if rest.get("away_b2b"):
            notes.append("away B2B")
        hr = rest.get("home_rest")
        ar = rest.get("away_rest")
        if hr is not None and ar is not None:
            notes.append(f"rest: home {hr}d / away {ar}d")
        points += 3
        fields.append(f"rest data ({', '.join(notes) if notes else 'normal rest'})")

    weather = ctx.get("weather", {})
    if isinstance(weather, dict) and weather:
        points += 2
        fields.append(f"weather: {weather.get('summary', 'available')}")

    pace = ctx.get("pace", {})
    if isinstance(pace, dict) and pace:
        points += 2
        fields.append("pace/efficiency stats")

    if points == 0:
        label = "NONE — context fetch failed entirely"
    elif points < 5:
        label = "SPARSE — limited data available"
    elif points < 12:
        label = "MODERATE — partial data available"
    else:
        label = "RICH — comprehensive data available"

    return label, points, fields


# ---------------------------------------------------------------------------
# Pipeline data formatter
# ---------------------------------------------------------------------------

def _build_pipeline_section(bet: dict) -> str:
    """
    Format the pre-computed pipeline fields stored in EVBetCache into a
    readable block for Claude.  These values are always available (computed
    during the hourly pipeline run) — they are the primary source of
    pick-specific signal and should be cited directly in the analysis.
    """
    game = bet.get("game", "")
    lines: list[str] = []

    # ── Model projections ─────────────────────────────────────────────────
    ph = bet.get("proj_home_score")
    pa = bet.get("proj_away_score")
    pt = bet.get("proj_total")
    pw = bet.get("proj_home_win_prob")
    if ph is not None and pa is not None and " @ " in game:
        away_t, home_t = [s.strip() for s in game.split(" @ ", 1)]
        proj_line = f"Projected score: {away_t} {pa:.1f} — {home_t} {ph:.1f}"
        if pt is not None:
            proj_line += f" (total {pt:.1f})"
        if pw is not None:
            proj_line += f" | Home win probability: {pw*100:.0f}%"
        lines.append(f"• {proj_line}")

    # ── Sharp / public money splits ───────────────────────────────────────
    bp = bet.get("bet_pct")
    mp = bet.get("money_pct")
    ss = bet.get("sharp_score")
    if bp is not None or mp is not None or ss is not None:
        parts = []
        if bp is not None:
            parts.append(f"{bp:.0f}% of bets")
        if mp is not None:
            parts.append(f"{mp:.0f}% of money")
        if ss is not None:
            sharpness = "high sharp interest" if ss >= 65 else ("moderate sharp interest" if ss >= 40 else "low sharp interest")
            parts.append(f"sharp score {ss:.0f}/100 ({sharpness})")
        lines.append(f"• Public/sharp splits: {' | '.join(parts)}")

    # ── Line movement (CLV signal) ────────────────────────────────────────
    opening = bet.get("opening_odds")
    current = bet.get("odds", 0)
    if opening and opening != current:
        open_str = f"+{opening}" if opening > 0 else str(opening)
        cur_str  = f"+{current}"  if current  > 0 else str(current)
        if opening > current:  # odds shortened toward us → sharp money confirming
            lines.append(f"• Line movement: {open_str} → {cur_str} (steam toward this side — sharp action confirmed)")
        else:
            lines.append(f"• Line movement: {open_str} → {cur_str} (line drifted against — model still shows edge; treat as context, not a disqualifier)")

    # ── Recent form (pipeline trend strings) ─────────────────────────────
    ht = bet.get("home_trend", "")
    at = bet.get("away_trend", "")
    if (ht or at) and " @ " in game:
        away_t, home_t = [s.strip() for s in game.split(" @ ", 1)]
        form_parts = []
        if at:
            form_parts.append(f"{away_t} (away): {at} last 10")
        if ht:
            form_parts.append(f"{home_t} (home): {ht} last 10")
        if form_parts:
            lines.append(f"• Recent form: {' | '.join(form_parts)}")

    # ── Model adjustment flags ────────────────────────────────────────────
    flags = bet.get("adj_flags", "")
    adj_prob = bet.get("adjusted_prob")
    base_prob = bet.get("true_prob", 0.0)
    if flags:
        flag_list = [f.strip() for f in flags.split("|") if f.strip()]
        adj_str = f" (adjusted true prob: {adj_prob*100:.1f}%)" if adj_prob and abs(adj_prob - base_prob) > 0.003 else ""
        lines.append(f"• Model context adjustments applied: {', '.join(flag_list)}{adj_str}")

    # ── Market-wide book odds ─────────────────────────────────────────────
    all_odds_raw = bet.get("all_book_odds", "")
    if all_odds_raw:
        try:
            all_odds = json.loads(all_odds_raw)
            if all_odds and isinstance(all_odds, dict):
                odds_parts = []
                for bk, v in list(all_odds.items())[:10]:
                    s = f"+{v}" if v > 0 else str(v)
                    odds_parts.append(f"{bk}: {s}")
                lines.append(f"• Odds across books: {' | '.join(odds_parts)}")
        except Exception:
            pass

    if not lines:
        return "(Pipeline data not available for this bet — rely on context data below.)"
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(bet: dict, ctx: dict) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt)."""
    is_prop = bet.get("is_prop", False)
    player_name = bet.get("player_name", "")
    market = bet.get("market", "h2h")
    team = bet.get("team", "")
    game = bet.get("game", "")
    odds = bet.get("odds", 0)
    true_prob = bet.get("true_prob", 0.0)
    ev_pct = bet.get("ev_percent", 0.0)
    point = bet.get("point")
    league = bet.get("league", "")

    implied_prob = round(_american_to_prob(odds) * 100, 1)
    true_prob_pct = round(true_prob * 100, 1)
    fair_odds_decimal = 1 / true_prob if true_prob > 0 else 0
    fair_odds_american = round((fair_odds_decimal - 1) * 100) if fair_odds_decimal >= 2 else round(-100 / (fair_odds_decimal - 1))
    edge_pct = round(true_prob_pct - implied_prob, 1)

    sign = "+" if odds > 0 else ""
    odds_str = f"{sign}{odds}"
    fair_sign = "+" if fair_odds_american > 0 else ""
    fair_odds_str = f"{fair_sign}{fair_odds_american}"

    if is_prop and player_name:
        direction = "Over" if str(team).lower().startswith("over") else "Under"
        bet_desc = f"{player_name} — {market.replace('_', ' ').title()} {direction} {point}"
    elif market == "spreads" and point is not None:
        # Always show explicit sign so Claude knows which side (e.g. +1.5 vs -1.5)
        point_str = f"+{point}" if point > 0 else str(point)
        bet_desc = f"{team} {point_str} (spread)"
    elif market == "totals" and point is not None:
        direction = "Over" if str(team).lower().startswith("over") else "Under"
        bet_desc = f"{direction} {point} (total)"
    elif point is not None:
        bet_desc = f"{team} {point} ({market})"
    else:
        bet_desc = f"{team} ({market})"

    # Context quality assessment
    ctx_quality, ctx_points, ctx_fields = _assess_context_quality(ctx)
    ctx_fields_str = ", ".join(ctx_fields) if ctx_fields else "none"

    if ctx_points == 0:
        log.warning("analyze_bet: context is EMPTY for bet id=%s (%s). Claude will have no real-world data.", bet.get("id"), game)
    elif ctx_points < 5:
        log.warning("analyze_bet: context is SPARSE (score=%d) for bet id=%s (%s). Fields: %s", ctx_points, bet.get("id"), game, ctx_fields_str)
    else:
        log.info("analyze_bet: context quality=%s (score=%d) for bet id=%s. Fields: %s", ctx_quality, ctx_points, bet.get("id"), ctx_fields_str)

    ctx_json = json.dumps(ctx, indent=2, default=str)[:14000]

    # Pipeline data — always available, primary signal source
    pipeline_section = _build_pipeline_section(bet)

    # Sport-specific analysis block
    sport_block = _SPORT_CONTEXT.get(league, """
**Sport-Specific Factors to Address (use data from context where available):**
- INJURIES: Check `injuries.home` and `injuries.away` — name any Out/Doubtful players and state their impact.
- REST/FATIGUE: Check `rest.home_b2b` / `rest.away_b2b` and `rest.home_rest` / `rest.away_rest` — name any B2B team.
- WEATHER: Check `weather` — wind, temperature, and conditions for outdoor games.
- Recent form: wins/losses in last 5-7 games from team history data
- Key matchup factors relevant to this market
- Market movement signals from the odds data
""")

    user_prompt = f"""## Bet Under Analysis

**Game:** {game}
**League:** {league}
**Bet:** {bet_desc}
**Book odds:** {odds_str}
**Model edge:** {true_prob_pct}% true probability vs. {implied_prob}% book-implied (+{edge_pct}% gap, {ev_pct}% EV)
**Fair value odds:** {fair_odds_str}

## Pipeline Signals (pre-computed, always reliable)

{pipeline_section}

## Live Context Data Quality

**Status:** {ctx_quality}
**Additional data:** {ctx_fields_str}

**Signal priority for your lead sentence:**
1. Real-world sport facts from the Live Context Data below: pitcher ERA/matchup history, team win streak, rest advantage, key injury, player prop trend. These are the most compelling to a bettor.
2. Sharp money % and model projections if context is sparse.
3. Line movement — weave in naturally as supporting evidence. If the line drifted against the bet, acknowledge it briefly as a risk and explain why the model edge persists despite it. Never lead with adverse line movement.

Never mention internal field names or API structures.

## Live Context Data

```json
{ctx_json}
```

**Stored enrichment guide — always check these keys if present in the JSON above:**
- `injuries.home` / `injuries.away` — Out/Doubtful players. A star absent = direct line impact; name them.
- `rest.home_b2b` / `rest.away_b2b` — true means that team played yesterday. Back-to-backs measurably lower cover rates; name the team.
- `rest.home_rest` / `rest.away_rest` — days since last game. Large rest advantages (≥2 days) matter.
- `weather` — outdoor games only (NFL/MLB). Extreme wind (>15 mph) suppresses scoring; heavy rain/snow affects totals.
- `pace.home_pace` / `pace.away_pace` — NBA possessions per 48. High-pace team vs. slow team = over/under edge.

{sport_block}

## Your Task

Write like a sharp bettor tipping off a friend. Someone clicks Analyze and needs to understand in 4–5 sentences exactly WHY this pick has edge and why it's worth placing.

**why_bet** (3–4 sentences, no section headers, no formula recitation):
Lead with the single sharpest real-world fact from the Live Context Data — pitcher ERA/matchup history, team form over last 10, rest advantage, player prop pace, injury to a key opponent. If real-world context is rich, that goes first. Then state the market angle plainly: the probability gap, why the book has mispriced this, and what the model is seeing that the market is missing. Add one supporting signal (sharp money, line steam, projections). If the line drifted against the bet, mention it briefly as a risk — don't lead with it. Keep it concise and direct.

**risk** (1 sentence): Name the specific player, scenario, or matchup factor that could make this bet lose. No generic disclaimers.

**edge_tag** (4–6 words): The sharpest description of why this bet has edge. Examples: "Sharp steam confirms the model" / "Pitcher struggles vs. this lineup" / "Books overpricing the favorite" / "Public fading the wrong side" / "Model projects comfortable cover."

**recommended_action**: "Strong Bet", "Moderate Bet", "Lean", or "Pass."

```json
{{
  "true_prob_refined": <float 0.0-1.0, blend model + context. Start from {round(true_prob, 3)}>,
  "confidence_score": <int 1-100. Rich pipeline signals (projections, sharp score ≥50, clear line steam) = 70-85. Both signals and context absent/contradictory = below 60. 80-100=high conviction, 60-79=moderate, 40-59=low, below 40=pass>,
  "kelly_full_pct": <float, full Kelly % capped at 8.0>,
  "kelly_fractional_pct": <float, 25% fractional Kelly %>,
  "ev_pct_refined": <float, EV% using your refined true_prob>,
  "context_quality": "{ctx_quality}",
  "analysis": {{
    "recommended_action": "<Strong Bet|Moderate Bet|Lean|Pass>",
    "edge_tag": "<4-6 words capturing the core edge>",
    "why_bet": "<3-4 sentences. No headers. Lead with the best real-world fact. Weave in market angle. Add one supporting fact.>",
    "risk": "<1 sentence naming a specific player, scenario, or matchup factor.>"
  }}
}}
```

Respond with ONLY the JSON object."""

    return _SYSTEM_PROMPT, user_prompt


# ---------------------------------------------------------------------------
# Output sanitizer — strips technical leakage before showing users
# ---------------------------------------------------------------------------

import re as _re

_TECHNICAL_TERMS = [
    r"events_sample\s*(array)?",
    r"team_history\s*(array|object|field)?",
    r"player_gamelogs?\s*(array)?",
    r"player_projections?\s*(object)?",
    r"market_odds?\s*(object)?",
    r"game_event\s*(object)?",
    r"context\s+JSON",
    r"context\s+data\s+(was\s+)?empty",
    r"MCP\s+server",
    r"Optimal\s+(Bet\s+)?MCP",
    r"API\s+response",
    r"array\s+was\s+(completely\s+)?empty",
    r"returned\s+(no|zero|null|empty)\s+(data|results?|context)",
    r"fetch(ing)?\s+failed",
    # Hypothetical "missing data" language
    r"to\s+gain\s+confidence",
    r"an\s+analyst\s+would\s+need",
    r"it\s+would\s+be\s+(helpful|useful|important)\s+to\s+(have|see|know)",
    r"would\s+need\s+(to\s+)?(confirm|see|know|have)",
    r"along\s+with\s+(each|the)\s+team.s",
    r"particularly\s+whether",
]
_TECHNICAL_RE = _re.compile("|".join(_TECHNICAL_TERMS), _re.IGNORECASE)

_SPARSE_FALLBACK = (
    "Live stats and recent form data weren't available for this matchup at the time of analysis. "
    "The edge is based on the model's probability estimate alone — treat confidence accordingly "
    "and consider checking recent news before placing this bet."
)

def _sanitize_context_text(text: str) -> str:
    """Replace any technical leakage with user-friendly language."""
    if not text:
        return text
    if _TECHNICAL_RE.search(text):
        log.warning("_sanitize_context_text: stripped technical language from contextual_validation.")
        # If the whole section is essentially an error message, replace entirely
        clean = _TECHNICAL_RE.sub("", text).strip()
        # If stripping gutted the content (< 60 chars left), use the fallback
        if len(clean) < 60:
            return _SPARSE_FALLBACK
        return clean
    return text


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def analyze_bet(bet: dict, optimal_client: Optional[OptimalClient] = None) -> Optional[dict]:
    """
    Generate AI analysis for a single +EV bet.

    Parameters
    ----------
    bet : dict
        Must contain: id, game, market, team, odds, true_prob, ev_percent,
        league. Optional: point, player_name, is_prop.
    optimal_client : OptimalClient, optional
        Reuse an existing client (avoids re-initialization in loops).

    Returns
    -------
    dict with keys:
        analysis          : str — full formatted analysis text
        confidence_score  : float (1-100)
        kelly_pct         : float — 25% fractional Kelly %
        true_prob_refined : float — Claude's refined probability estimate
        ev_pct_refined    : float
        raw               : dict — full parsed Claude response
    Returns None if the API call fails.
    """
    if optimal_client is None:
        optimal_client = OptimalClient()

    # Fetch live context
    ctx = {}
    try:
        ctx = _build_context(bet, optimal_client)
    except Exception as exc:
        log.warning("analyze_bet: context fetch error (non-fatal): %s", exc)

    # Merge stored enrichment (injuries, rest/B2B, weather, pace) computed at
    # pipeline time by _enrich_game_contexts().  These are always reliable —
    # inject directly into ctx so Claude sees them alongside live data.
    stored_gc_raw = bet.get("game_context")
    if stored_gc_raw:
        try:
            stored_gc = json.loads(stored_gc_raw)
            if isinstance(stored_gc, dict):
                for key in ("injuries", "rest", "weather", "pace"):
                    if stored_gc.get(key) and key not in ctx:
                        ctx[key] = stored_gc[key]
                log.info(
                    "analyze_bet: merged stored enrichment keys %s for bet id=%s",
                    [k for k in ("injuries", "rest", "weather", "pace") if ctx.get(k)],
                    bet.get("id"),
                )
        except Exception as exc:
            log.warning("analyze_bet: failed to parse stored game_context: %s", exc)

    system_prompt, user_prompt = _build_prompt(bet, ctx)

    # Call Claude — no extended thinking, keeps latency predictable on Railway
    try:
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            timeout=30.0,   # hard 30-second cap so Railway never times out
        )
        message = client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        log.error("analyze_bet: Claude API call failed: %s", exc)
        return None

    # Extract text response
    response_text = ""
    for block in message.content:
        if getattr(block, "type", None) == "text" and hasattr(block, "text"):
            response_text = block.text.strip()
            break
        elif isinstance(block, str):
            response_text = block.strip()
            break

    if not response_text:
        log.warning("analyze_bet: Claude returned no text for bet id=%s", bet.get("id"))
        return None

    # Parse JSON
    try:
        # Strip markdown fences if Claude adds them despite instructions
        clean = response_text
        if clean.startswith("```"):
            clean = clean.split("```", 2)[1]
            if clean.startswith("json"):
                clean = clean[4:]
            clean = clean.rstrip("`").strip()
        raw = json.loads(clean)
    except json.JSONDecodeError as exc:
        log.error("analyze_bet: failed to parse Claude JSON: %s\nResponse: %s", exc, response_text[:500])
        return None

    # Extract fields with fallbacks
    analysis_obj = raw.get("analysis", {})
    confidence = float(raw.get("confidence_score", 50))
    # Always recompute Kelly from the model's raw true_prob — never trust Claude's
    # kelly_fractional_pct, which is based on its own true_prob_refined and can
    # diverge from what the card's metric cell shows.
    kelly_frac = _kelly(bet.get("true_prob", 0.5), bet.get("odds", -110))
    true_prob_refined = float(raw.get("true_prob_refined", bet.get("true_prob", 0.5)))
    ev_refined = float(raw.get("ev_pct_refined", bet.get("ev_percent", 0.0)))
    ctx_quality = raw.get("context_quality", "")

    # Format the human-readable analysis block
    why_bet  = _sanitize_context_text(analysis_obj.get("why_bet", ""))
    risk     = analysis_obj.get("risk", "")
    rec      = analysis_obj.get("recommended_action", "Moderate Bet")
    edge_tag = analysis_obj.get("edge_tag", "")

    formatted = (
        f"**{rec}**\n\n"
        f"{why_bet}"
    )

    return {
        "analysis":          formatted,
        "confidence_score":  confidence,
        "kelly_pct":         kelly_frac,
        "true_prob_refined": true_prob_refined,
        "ev_pct_refined":    ev_refined,
        "context_quality":   ctx_quality,
        "edge_tag":          edge_tag,
        "raw":               raw,
    }


# ---------------------------------------------------------------------------
# Card summary — short inline analysis using Haiku (pre-generated at cache time)
# ---------------------------------------------------------------------------

def generate_card_summary(bet: dict) -> Optional[str]:
    """
    Generate a 2-3 sentence plain-English summary of why a bet has edge.
    Uses claude-haiku-3-5 for cost efficiency (~$0.03 per full cache batch).
    Returns the summary string, or None on failure.
    """
    # ── Assemble the signal block ─────────────────────────────────────────
    true_prob    = float(bet.get("true_prob") or 0.5)
    implied_prob = float(bet.get("implied_prob") or 0.0)
    ev_pct       = float(bet.get("ev_percent") or 0.0)
    odds         = int(bet.get("odds") or -110)
    opening_odds = bet.get("opening_odds")
    bet_pct      = bet.get("bet_pct")
    money_pct    = bet.get("money_pct")
    sharp_score  = bet.get("sharp_score")
    home_trend   = (bet.get("home_trend") or "").strip()
    away_trend   = (bet.get("away_trend") or "").strip()
    game         = (bet.get("game") or "").strip()
    team         = (bet.get("team") or "").strip()
    market       = (bet.get("market") or "h2h").strip()
    is_prop      = bool(bet.get("is_prop"))
    player_name  = (bet.get("player_name") or "").strip()
    point        = bet.get("point")
    adj_flags    = (bet.get("adj_flags") or "").strip()
    proj_home_wp = bet.get("proj_home_win_prob")
    proj_total   = bet.get("proj_total")

    if not implied_prob and odds:
        implied_prob = (100.0 / (odds + 100.0)) if odds > 0 else (abs(odds) / (abs(odds) + 100.0))

    odds_str = f"+{odds}" if odds > 0 else str(odds)
    true_pct = round(true_prob * 100, 1)
    impl_pct = round(implied_prob * 100, 1)
    edge_pp  = round((true_prob - implied_prob) * 100, 1)

    if is_prop and player_name:
        bet_label = f"{player_name} — {team} ({market})"
    elif market == "spreads" and point is not None:
        pt_str = f"+{point}" if point > 0 else str(point)
        bet_label = f"{team} {pt_str}"
    elif market == "totals" and point is not None:
        bet_label = f"{team} {point}"
    else:
        bet_label = team

    signals = [
        f"Bet: {bet_label} at {odds_str} in {game}",
        f"EV: +{ev_pct:.1f}% | Model probability: {true_pct}% vs {impl_pct}% implied ({edge_pp:+.1f}pp edge)",
    ]

    if sharp_score is not None:
        level = "high" if sharp_score >= 65 else ("moderate" if sharp_score >= 40 else "low")
        signals.append(f"Sharp signal: {sharp_score:.0f}/100 ({level})")
    if money_pct is not None and bet_pct is not None:
        signals.append(f"Betting splits: {bet_pct:.0f}% of bets / {money_pct:.0f}% of money on this side")
    if opening_odds and opening_odds != odds:
        open_str = f"+{opening_odds}" if opening_odds > 0 else str(opening_odds)
        direction = "steamed toward us (CLV+)" if opening_odds > odds else "drifted away"
        signals.append(f"Line movement: {open_str} → {odds_str} — {direction}")
    if home_trend or away_trend:
        if " @ " in game:
            away_t, home_t = [s.strip() for s in game.split(" @ ", 1)]
            if away_trend:
                signals.append(f"Recent form: {away_t} {away_trend} (away)")
            if home_trend:
                signals.append(f"Recent form: {home_t} {home_trend} (home)")
    if proj_home_wp is not None:
        signals.append(f"Model projects {round(proj_home_wp*100)}% home win probability")
    if proj_total is not None:
        signals.append(f"Model projected total: {proj_total:.1f}")
    if adj_flags:
        signals.append(f"Model adjustments: {adj_flags.replace('|', ', ')}")

    signal_block = "\n".join(signals)

    # ── Real-world context (injuries, rest, weather, pace) ────────────────
    game_context_raw = bet.get("game_context")
    if game_context_raw:
        try:
            ctx = json.loads(game_context_raw)
            ctx_lines: list[str] = []

            # Injuries — only Out / Doubtful are meaningful
            for side in ("home", "away"):
                key_out = [
                    p["player"]
                    for p in ctx.get("injuries", {}).get(side, [])
                    if p.get("status") in ("Out", "Doubtful")
                ]
                if key_out:
                    ctx_lines.append(
                        f"{side.title()} injuries (Out/Doubtful): {', '.join(key_out[:3])}"
                    )

            # Rest advantage / back-to-back
            rest = ctx.get("rest", {})
            if rest.get("home_b2b"):
                ctx_lines.append("Home team on back-to-back (0 days rest)")
            if rest.get("away_b2b"):
                ctx_lines.append("Away team on back-to-back (0 days rest)")
            elif (
                rest.get("home_days_rest") is not None
                and rest.get("away_days_rest") is not None
            ):
                h, a = rest["home_days_rest"], rest["away_days_rest"]
                if abs(h - a) >= 2:
                    adv = "Home" if h > a else "Away"
                    ctx_lines.append(
                        f"Rest edge: {adv} team ({max(h, a)}d rest vs {min(h, a)}d for opponent)"
                    )

            # Weather (outdoor games only)
            weather = ctx.get("weather", {})
            if weather:
                ctx_lines.append(f"Weather: {weather['summary']}")

            # Pace / scoring efficiency
            pace = ctx.get("pace", {})
            if "home_pace" in pace and "away_pace" in pace:
                ctx_lines.append(
                    f"Pace: Home {pace['home_pace']:.0f} vs Away {pace['away_pace']:.0f} poss/48"
                )
            elif "home_goals_pg" in pace and "away_goals_pg" in pace:
                ctx_lines.append(
                    f"Scoring pace: Home {pace['home_goals_pg']:.1f} vs Away {pace['away_goals_pg']:.1f} goals/gm"
                )
            elif "home_runs_pg" in pace and "away_runs_pg" in pace:
                ctx_lines.append(
                    f"Scoring pace: Home {pace['home_runs_pg']:.1f} vs Away {pace['away_runs_pg']:.1f} runs/gm"
                )

            if ctx_lines:
                signal_block += "\n\nReal-world context:\n" + "\n".join(
                    f"- {line}" for line in ctx_lines
                )
        except Exception:
            pass  # bad JSON or unexpected shape — just skip context

    prompt = (
        "You are a concise sports betting analyst. Given these signals for a +EV bet, "
        "write EXACTLY 2-3 sentences explaining the key edge. "
        "Be specific and cite the data. No bullet points, no markdown, no filler phrases. "
        "Plain sentences only.\n\n"
        f"{signal_block}"
    )

    try:
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            timeout=15.0,
        )
        message = client.messages.create(
            model="claude-haiku-3-5",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in message.content:
            if getattr(block, "type", None) == "text":
                text = block.text.strip()
                break
        return text or None
    except Exception as exc:
        log.warning("generate_card_summary failed for bet id=%s: %s", bet.get("id"), exc)
        return None


# ---------------------------------------------------------------------------
# Rule-based analysis fallback (no API call)
# ---------------------------------------------------------------------------

def rule_based_analyze_bet(bet: dict) -> dict:
    """
    Generate structured analysis purely from pipeline data fields.
    No external API call — used as fallback when the AI service is unavailable.

    Draws on: EV%, true_prob vs implied_prob, sharp money signals,
    CLV direction, line movement, team trends, and score projections.
    """
    # ── Extract fields ────────────────────────────────────────────────────────
    true_prob    = float(bet.get("true_prob") or 0.5)
    implied_prob = float(bet.get("implied_prob") or 0.0)
    ev_pct       = float(bet.get("ev_percent") or 0.0)
    odds         = int(bet.get("odds") or -110)
    opening_odds = bet.get("opening_odds")
    bet_pct      = bet.get("bet_pct")
    money_pct    = bet.get("money_pct")
    sharp_score  = bet.get("sharp_score")
    home_trend   = (bet.get("home_trend") or "").strip()
    away_trend   = (bet.get("away_trend") or "").strip()
    game         = (bet.get("game") or "").strip()
    team         = (bet.get("team") or "").strip()
    is_prop      = bool(bet.get("is_prop"))
    player_name  = (bet.get("player_name") or "").strip()
    adj_flags    = (bet.get("adj_flags") or "").strip()
    proj_home_win_prob = bet.get("proj_home_win_prob")
    proj_total         = bet.get("proj_total")

    # Derive implied_prob if not pre-computed
    if not implied_prob:
        if odds > 0:
            implied_prob = 100.0 / (odds + 100.0)
        else:
            implied_prob = abs(odds) / (abs(odds) + 100.0)

    prob_edge_pp = (true_prob - implied_prob) * 100.0
    kelly_frac   = _kelly(true_prob, odds)

    # ── Confidence score (0–100) ──────────────────────────────────────────────
    confidence = 50.0

    if ev_pct >= 10:   confidence += 15
    elif ev_pct >= 7:  confidence += 10
    elif ev_pct >= 5:  confidence += 7
    elif ev_pct >= 3:  confidence += 4

    if money_pct is not None:
        mp = float(money_pct)
        if mp >= 70:   confidence += 12
        elif mp >= 60: confidence += 7
        elif mp >= 55: confidence += 3
        elif mp < 40:  confidence -= 5

    if sharp_score is not None:
        ss = float(sharp_score)
        if ss >= 75:   confidence += 10
        elif ss >= 60: confidence += 6
        elif ss < 30:  confidence -= 5

    clv_favorable = False
    if opening_odds is not None and opening_odds != odds:
        # Line shortened (opening > current) = money flowed onto this bet = positive CLV
        # e.g. +122 → +117: 122 > 117 → positive; -130 → -140: -130 > -140 → positive
        if opening_odds > odds:
            confidence   += 8
            clv_favorable = True
        else:
            confidence -= 3

    confidence = max(20.0, min(90.0, confidence))

    # ── Recommended action ────────────────────────────────────────────────────
    if ev_pct >= 8:    rec = "Strong Bet"
    elif ev_pct >= 5:  rec = "Moderate Bet"
    elif ev_pct >= 3:  rec = "Value Bet"
    else:              rec = "Lean"

    # ── Edge tag ──────────────────────────────────────────────────────────────
    has_sharp = sharp_score is not None and float(sharp_score or 0) >= 65
    if clv_favorable and has_sharp: edge_tag = "CLV + Sharp"
    elif clv_favorable:             edge_tag = "CLV Edge"
    elif has_sharp:                 edge_tag = "Sharp Money"
    elif ev_pct >= 7:               edge_tag = "High Edge"
    else:                           edge_tag = "Model Edge"

    # ── Build narrative why-bet text ──────────────────────────────────────────
    # The visual panel renders the raw numbers (bars, chips), so the narrative
    # explains what the signals MEAN together — not a list of percentages.
    true_pct = round(true_prob * 100, 1)
    edge_pp  = round(prob_edge_pp, 1)
    sign     = "+" if edge_pp >= 0 else ""

    subject = (f"{player_name} ({team})" if is_prop and player_name
               else team if team else "this side")

    sentences = []

    # Core edge sentence
    if edge_pp >= 5:
        sentences.append(
            f"The market is significantly underpricing **{subject}** — "
            f"the model sees a **{sign}{edge_pp}pp gap** that the book hasn't closed."
        )
    else:
        sentences.append(
            f"The model identifies a **{sign}{edge_pp}pp mispricing** on **{subject}** "
            f"that generates repeatable positive expected value."
        )

    # Sharp money interpretation
    mp = float(money_pct) if money_pct is not None else None
    bp = float(bet_pct)   if bet_pct   is not None else None
    ss = float(sharp_score) if sharp_score is not None else None

    if mp is not None and bp is not None and mp > bp + 8:
        sentences.append(
            f"Reverse line movement: only {round(bp)}% of tickets are on {subject.split('(')[0].strip()}, "
            f"but {round(mp)}% of the money is — a classic sharp bettor signature."
        )
    elif mp is not None and mp >= 65:
        sentences.append(
            f"Significant dollar flow ({round(mp)}%) has landed on this side, "
            f"suggesting professional alignment with the model's edge."
        )
    elif mp is not None and mp < 38:
        sentences.append(
            f"The public is fading this side ({round(mp)}% of money), "
            f"but the model's probability advantage persists — sharp value often hides in contrarian spots."
        )
    elif ss is not None and ss >= 65:
        sentences.append(
            f"Sharp money indicators are elevated (score {round(ss)}/100), "
            f"consistent with informed bettor activity on this line."
        )

    # Line movement interpretation
    if opening_odds is not None and opening_odds != odds:
        op_str  = f"+{opening_odds}" if opening_odds > 0 else str(opening_odds)
        cur_str = f"+{odds}" if odds > 0 else str(odds)
        if clv_favorable:
            sentences.append(
                f"The line has already moved in our favor ({op_str} → {cur_str}), "
                f"confirming smart money is on the same side — getting in now still captures CLV."
            )
        else:
            sentences.append(
                f"The line has moved against since open ({op_str} → {cur_str}). "
                f"The model edge remains, but the window is narrowing — act before further movement."
            )

    # Game context (game bets only)
    if not is_prop and game and " @ " in game:
        away_team, home_team = game.split(" @ ", 1)
        away_team = away_team.strip()
        home_team = home_team.strip()
        if proj_home_win_prob is not None:
            hwp = round(float(proj_home_win_prob) * 100, 1)
            awp = round(100.0 - hwp, 1)
            bet_team_is_home = team and home_team.lower().endswith(team.lower().split()[-1].lower())
            model_wp = hwp if bet_team_is_home else awp
            sentences.append(
                f"The game model projects a {round(model_wp)}% win probability for the bet side"
                + (f", with a projected total of {round(float(proj_total), 1)}." if proj_total else ".")
            )
        if home_trend and away_trend:
            sentences.append(f"Recent form — {home_team}: {home_trend} · {away_team}: {away_trend}.")

    # Risk/closing sentence
    risk_notes = []
    if opening_odds is not None and not clv_favorable and opening_odds != odds:
        risk_notes.append("line has moved against this bet")
    if mp is not None and mp < 40:
        risk_notes.append("majority of money is on the other side")
    if ev_pct < 4:
        risk_notes.append("slim edge — use reduced unit size")
    if adj_flags:
        risk_notes.append("model adjustments applied: " + adj_flags.replace("|", ", ").replace("_", " "))

    if risk_notes:
        sentences.append("⚠ Note: " + "; ".join(risk_notes) + ".")

    why_bet   = " ".join(sentences)
    formatted = f"**{rec}**\n\n{why_bet}"

    return {
        "analysis":           formatted,
        "confidence_score":   round(confidence),
        "kelly_pct":          kelly_frac,
        "true_prob_refined":  true_prob,
        "ev_pct_refined":     ev_pct,
        "context_quality":    "rule_based",
        "edge_tag":           edge_tag,
        "raw": {
            "analysis": {
                "why_bet":            why_bet,
                "risk":               "; ".join(risk_notes) if risk_notes else "None",
                "recommended_action": rec,
                "edge_tag":           edge_tag,
            }
        },
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    sample_bet = {
        "id": 999,
        "game": "Orlando Magic @ Dallas Mavericks",
        "league": "basketball_nba",
        "market": "h2h",
        "team": "Orlando Magic",
        "odds": 140,
        "true_prob": 0.385,
        "ev_percent": 5.4,
        "point": None,
        "player_name": None,
        "is_prop": False,
    }

    print(f"Analyzing: {sample_bet['game']} — {sample_bet['team']}")
    result = analyze_bet(sample_bet)

    if result:
        print(f"\nConfidence: {result['confidence_score']}/100")
        print(f"Kelly (25%): {result['kelly_pct']}%")
        print(f"Refined prob: {result['true_prob_refined']:.3f}")
        print(f"\n--- Analysis ---\n{result['analysis']}")
    else:
        print("Analysis failed.")
        sys.exit(1)
