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
    1. search_teams → get_team_history (structured data, preferred)
    2. query() fallback (freeform, used when strategy 1 fails)
    Returns the form data or None if both fail.
    """
    # Strategy 1: search → lookup
    try:
        teams = client.search_teams(team_name, league=league_key) or []
        if isinstance(teams, list) and teams:
            team_id = (
                teams[0].get("team_id")
                or teams[0].get("id")
                or teams[0].get("teamId")
                or teams[0].get("team_key")
            )
            if team_id:
                hist = client.get_team_history(str(team_id), last_n=10)
                if hist:
                    log.info("Optimal context: team history fetched via search for %s (team_id=%s)", team_name, team_id)
                    return hist
                else:
                    log.warning("Optimal context: get_team_history returned nothing for %s (team_id=%s)", team_name, team_id)
            else:
                log.warning("Optimal context: search_teams returned no usable team_id for %s. Keys found: %s",
                            team_name, list(teams[0].keys()) if teams else [])
        else:
            log.warning("Optimal context: search_teams returned no results for %s in %s", team_name, league_key)
    except Exception as exc:
        log.warning("Optimal context: search/history chain failed for %s: %s", team_name, exc)

    # Strategy 2: freeform query fallback
    try:
        q = f"Recent form, wins, losses, and results for the {team_name} in {league_key} over the last 10 games"
        result = client.query(q)
        if result:
            log.info("Optimal context: team form fetched via query() fallback for %s", team_name)
            return {"query_result": result, "source": "freeform_query"}
        else:
            log.warning("Optimal context: query() fallback also returned nothing for %s", team_name)
    except Exception as exc:
        log.warning("Optimal context: query() fallback failed for %s: %s", team_name, exc)

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

    # Derive game_date (YYYY-MM-DD in ET) from commence_time so get_events
    # targets the correct calendar day — essential for tomorrow's bets.
    game_date: Optional[str] = None
    ct = bet.get("commence_time")
    if ct is not None:
        try:
            from zoneinfo import ZoneInfo
            if hasattr(ct, "astimezone"):
                game_date = ct.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
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

    # ── 2. Recent form for both teams (with fallback) ─────────────────────
    if " @ " in game_str:
        away_team, home_team = game_str.split(" @ ", 1)
        for team_name in [away_team.strip(), home_team.strip()]:
            form = _fetch_team_form(team_name, league_key, client)
            if form is not None:
                ctx.setdefault("team_history", {})[team_name] = form

    if not ctx.get("team_history"):
        log.warning("Optimal context: no team history captured for game='%s'", game_str)

    # ── 3. Player context (props only) ────────────────────────────────────
    if is_prop and player_name:
        try:
            players = client.search_players(player_name, league=league_key) or []
            if isinstance(players, list) and players:
                player_id = (
                    players[0].get("player_id")
                    or players[0].get("id")
                    or players[0].get("playerId")
                )
                if player_id:
                    gamelogs = client.get_player_gamelogs(player_id, last_n=10)
                    if gamelogs:
                        ctx["player_gamelogs"] = gamelogs
                    else:
                        log.warning("Optimal context: player gamelogs empty for %s (id=%s)", player_name, player_id)

                    game_event = ctx.get("game_event", {})
                    game_id = game_event.get("game_id") or game_event.get("id") if game_event else None
                    if game_id:
                        proj = client.get_player_projections(player_id, game_id=game_id)
                        if proj:
                            ctx["player_projections"] = proj
                else:
                    log.warning("Optimal context: no player_id found for %s. Keys: %s",
                                player_name, list(players[0].keys()) if players else [])
            else:
                # Fallback: query for player recent stats
                q = f"Recent stats and performance for {player_name} in {league_key} last 10 games"
                result = client.query(q)
                if result:
                    ctx["player_form_query"] = {"query_result": result, "source": "freeform_query"}
                    log.info("Optimal context: player form via query() fallback for %s", player_name)
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
- PLAYOFF SEEDING: If either team is within 2 games of a seed boundary or home-court advantage, open with that scenario and both seeds — this is the lead.
- BACK-TO-BACK: Name the team explicitly if confirmed. A fatigued team on a B2B typically covers at a materially lower rate.
- SHARP STEAM + FORM: If sharp score ≥50 and line moved toward this side, lead with that. Cite the last-10 record and streak.
- PROPS: State the player's actual hit rate vs. this line from gamelogs (e.g. "Over in 7 of last 10 = 70% vs. 54% implied"). This is what makes or breaks the prop case.
Skip anything not supported by the data provided.
""",

    "icehockey_nhl": """
Lead with the single strongest fact for this bet (use only what's in context):
- PLAYOFF STAKES: Conference rank, points, gap to wild card or elimination — if meaningful, this is the lead. State exactly what tonight determines.
- GOALTENDER MATCHUP: Name both confirmed starters and state GAA / save%. If unconfirmed, say so.
- FORM: Last-10 record and current streak for each team.
Skip anything not in the data.
""",

    "baseball_mlb": """
Lead with the single strongest fact for this bet (use only what's in context):
- PITCHER VS. THIS TEAM (highest priority if pitcher_vs_team is present): State the starter's record, ERA, and number of starts against this specific opponent. This is more predictive than season ERA.
- PITCHER SEASON STATS: ERA, WHIP. Mention the last start result if available.
- SERIES CONTEXT: Sweep scenario or series lead — strong motivational signal.
- TEAM FORM: Current streak and last-10 record.
Skip anything not in the data.
""",

    "soccer_epl": """
Lead with the single strongest fact: table stakes (relegation, title race, top-4 — with specific points gap) or form. State last 5 W/D/L. Named injuries if present in context.
""",

    "soccer_spain_la_liga": """
Lead with La Liga table stakes (title, UCL fight, relegation) with specific points gap if applicable. State last 5 form. Named absences from context only.
""",

    "soccer_germany_bundesliga": """
Lead with Bundesliga table stakes if applicable. State last 5 form and current streak.
""",

    "soccer_usa_mls": """
State last 5 form and current streak. Mention playoff positioning if within 3 points. MLS home advantage is meaningful — note home/away record split if in the data.
""",

    "soccer_uefa_champs_league": """
If knockout second leg: state the aggregate score and exactly what each team needs to advance — this is the lead, and the most important fact. Suspension risk (one yellow from a ban). Last 5 form.
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
        if opening > current:  # odds shortened toward us → sharp steam
            lines.append(f"• Line movement: {open_str} → {cur_str} — steamed in our direction (CLV+, confirms sharp action)")
        else:
            lines.append(f"• Line movement: {open_str} → {cur_str} — drifted away from us (monitor for steam reversal)")

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
- Recent form: wins/losses in last 5-7 games from team history data
- Key matchup factors relevant to this market
- Any injury or availability concerns visible in the data
- Market movement signals from the odds data
""")

    user_prompt = f"""## Bet Under Analysis

**Game:** {game}
**League:** {league}
**Bet:** {bet_desc}
**Book odds:** {odds_str}
**Model edge:** {true_prob_pct}% true probability vs. {implied_prob}% book-implied (+{edge_pct}% gap, {ev_pct}% EV)
**Fair value odds:** {fair_odds_str}

## Key Signals (use these as the factual basis — always reliable)

{pipeline_section}

Sharp money splits, line movement, and model projections are the strongest signals. Weave them into the narrative naturally.

## Live Context Data Quality

**Status:** {ctx_quality}
**Additional data:** {ctx_fields_str}

Use as supplementary support. If sparse, lean on the key signals above. Never mention internal field names or API structures.

## Live Context Data

```json
{ctx_json}
```

{sport_block}

## Your Task

Write like a sharp bettor tipping off a friend. Someone clicks Analyze and needs to understand in 4–5 sentences exactly WHY this pick has edge and why it's worth placing.

**why_bet** (3–4 sentences, no section headers, no formula recitation):
Lead with the single sharpest real-world fact you have — a pitcher's terrible history against this lineup, sharp money steaming the line, a specific playoff seeding battle, a player hitting this prop at 70% vs. 54% implied. Then weave in the market angle plainly: what the model sees vs. what the book is pricing, and why that gap exists. Add one supporting fact that makes the case stronger. Keep it concise and direct.

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
    kelly_frac = float(raw.get("kelly_fractional_pct", _kelly(bet.get("true_prob", 0.5), bet.get("odds", -110))))
    true_prob_refined = float(raw.get("true_prob_refined", bet.get("true_prob", 0.5)))
    ev_refined = float(raw.get("ev_pct_refined", bet.get("ev_percent", 0.0)))
    ctx_quality = raw.get("context_quality", "")

    # Format the human-readable analysis block
    why_bet  = _sanitize_context_text(analysis_obj.get("why_bet", ""))
    risk     = analysis_obj.get("risk", "")
    rec      = analysis_obj.get("recommended_action", "Moderate Bet")
    edge_tag = analysis_obj.get("edge_tag", "")

    # Prepend a data-quality notice if context was thin
    ctx_notice = ""
    if ctx_quality and ("SPARSE" in ctx_quality or "NONE" in ctx_quality):
        ctx_notice = "⚠️ *Live data was limited for this pick — treat confidence accordingly.*\n\n"

    formatted = (
        f"{ctx_notice}"
        f"**{rec}**\n\n"
        f"{why_bet}\n\n"
        f"⚠️ {risk}"
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
