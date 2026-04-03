"""
models/ai_analyzer.py — AI-powered bet analysis using Claude + Optimal data.

For each +EV bet, this module:
1. Fetches live context from the Optimal Bet MCP server (projections, team
   history, recent form, market consensus).
2. Calls Claude claude-opus-4-6 with adaptive thinking to generate a structured
   analysis including:
   - Improved true probability estimate
   - Confidence score (1–100)
   - Kelly criterion sizing (full + 25% fractional)
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

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL = "claude-opus-4-6"

# League → Optimal league key mapping
_LEAGUE_MAP = {
    "basketball_nba":          "NBA",
    "baseball_mlb":            "MLB",
    "icehockey_nhl":           "NHL",
    "basketball_ncaab":        "NCAAB",
    "soccer_epl":              "EPL",
    "soccer_spain_la_liga":    "La Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_usa_mls":          "MLS",
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

def _build_context(bet: dict, client: OptimalClient) -> dict:
    """
    Fetch relevant live data from the Optimal MCP server for the given bet.
    Returns a dict of context sections to pass to Claude.
    Failures are caught silently so analysis can still proceed with partial data.
    """
    ctx: dict = {}
    league_key = _LEAGUE_MAP.get(bet.get("league", ""), "NBA")
    game_str = bet.get("game", "")
    is_prop = bet.get("is_prop", False)
    player_name = bet.get("player_name")
    team = bet.get("team", "")
    market = bet.get("market", "h2h")

    # ── 1. Upcoming events to find team IDs ──────────────────────────────
    try:
        events = client.get_events(league_key) or []
        if isinstance(events, list):
            ctx["events_sample"] = events[:10]
            # Try to find the specific game
            home, away = "", ""
            if " @ " in game_str:
                away, home = game_str.split(" @ ", 1)
            for ev in events:
                ev_str = str(ev).lower()
                if home.lower() in ev_str or away.lower() in ev_str:
                    ctx["game_event"] = ev
                    break
    except Exception as exc:
        log.debug("Optimal context: events fetch failed: %s", exc)

    # ── 2. Team history for both teams ────────────────────────────────────
    if " @ " in game_str:
        away_team, home_team = game_str.split(" @ ", 1)
        for team_name in [away_team.strip(), home_team.strip()]:
            try:
                teams = client.search_teams(team_name, league=league_key) or []
                if isinstance(teams, list) and teams:
                    team_id = teams[0].get("team_id") or teams[0].get("id")
                    if team_id:
                        hist = client.get_team_history(team_id, last_n=7)
                        ctx.setdefault("team_history", {})[team_name] = hist
            except Exception as exc:
                log.debug("Optimal context: team history failed for %s: %s", team_name, exc)

    # ── 3. Player context (props only) ────────────────────────────────────
    if is_prop and player_name:
        try:
            players = client.search_players(player_name, league=league_key) or []
            if isinstance(players, list) and players:
                player_id = players[0].get("player_id") or players[0].get("id")
                if player_id:
                    gamelogs = client.get_player_gamelogs(player_id, last_n=10)
                    ctx["player_gamelogs"] = gamelogs

                    game_event = ctx.get("game_event", {})
                    game_id = game_event.get("game_id") or game_event.get("id") if game_event else None
                    if game_id:
                        proj = client.get_player_projections(player_id, game_id=game_id)
                        ctx["player_projections"] = proj
        except Exception as exc:
            log.debug("Optimal context: player data failed for %s: %s", player_name, exc)

    # ── 4. Market consensus odds ──────────────────────────────────────────
    try:
        game_event = ctx.get("game_event", {})
        game_id = game_event.get("game_id") or game_event.get("id") if game_event else None
        if game_id:
            odds_data = client.get_game_odds(game_id)
            ctx["market_odds"] = odds_data
    except Exception as exc:
        log.debug("Optimal context: market odds failed: %s", exc)

    return ctx


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(bet: dict, ctx: dict) -> str:
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
    kelly_full = _kelly(true_prob, odds, fraction=1.0)
    kelly_frac = _kelly(true_prob, odds, fraction=0.25)

    sign = "+" if odds > 0 else ""
    odds_str = f"{sign}{odds}"

    if is_prop and player_name:
        bet_desc = f"{player_name} — {market.replace('_', ' ').title()} {point} ({'Over' if 'Over' in team else 'Under'})"
    elif point is not None:
        side = "Over" if "Over" in team else ("Under" if "Under" in team else team)
        bet_desc = f"{side} {point} ({market})"
    else:
        bet_desc = f"{team} ({market})"

    ctx_json = json.dumps(ctx, indent=2, default=str)[:8000]  # guard token budget

    prompt = f"""You are a professional sports betting analyst with deep expertise in statistical modeling, market analysis, and sports analytics.

## Bet Under Analysis

**Game:** {game}
**League:** {league}
**Bet:** {bet_desc}
**Book odds:** {odds_str}
**Book implied probability:** {implied_prob}%
**Model no-vig true probability:** {true_prob_pct}%
**Current model EV%:** {ev_pct}%

## Live Context Data (from Optimal Bet MCP)

```json
{ctx_json}
```

## Your Task

Analyze this +EV bet opportunity thoroughly. Using both the mathematical model output AND the live context data above, produce a structured JSON response with the following fields:

```json
{{
  "true_prob_refined": <float, 0.0-1.0, your refined probability estimate blending model + context>,
  "confidence_score": <int 1-100, overall confidence in this pick>,
  "kelly_full_pct": <float, full Kelly % of bankroll>,
  "kelly_fractional_pct": <float, 25% fractional Kelly % of bankroll>,
  "ev_pct_refined": <float, EV% using your refined true_prob>,
  "analysis": {{
    "summary": "<1-2 sentence executive summary of why this is a good bet>",
    "mathematical_justification": "<3-5 sentence detailed explanation of the math: no-vig calculation, edge over the book, why the model is right>",
    "contextual_validation": "<3-5 sentence real-world context: recent form, matchup factors, injury situation, market movement, sharp money signals>",
    "risk_factors": "<1-3 sentence honest assessment of what could go wrong>",
    "recommended_action": "<'Strong Bet', 'Moderate Bet', or 'Lean' based on confidence_score and EV%>"
  }}
}}
```

Guidelines:
- Use the live context data to validate or adjust the model probability. If context strongly disagrees with the model, lower confidence_score.
- The confidence_score reflects how much you trust this pick: 80-100 = high conviction, 60-79 = moderate, 40-59 = low confidence, below 40 = pass.
- Kelly sizing should use your refined true_prob (not the model's). Cap kelly_full_pct at 8.0 (never recommend betting >8% of bankroll).
- Be honest in risk_factors — every bet has risks.
- Respond with ONLY the JSON object, no preamble or markdown fencing.
"""
    return prompt


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

    prompt = _build_prompt(bet, ctx)

    # Call Claude
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        message = client.messages.create(
            model=_MODEL,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        log.error("analyze_bet: Claude API call failed: %s", exc)
        return None

    # Extract text response (skip thinking blocks)
    response_text = ""
    for block in message.content:
        if hasattr(block, "text"):
            response_text = block.text.strip()
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

    # Format the human-readable analysis block
    summary = analysis_obj.get("summary", "")
    math_just = analysis_obj.get("mathematical_justification", "")
    ctx_valid = analysis_obj.get("contextual_validation", "")
    risk = analysis_obj.get("risk_factors", "")
    rec = analysis_obj.get("recommended_action", "Moderate Bet")

    formatted = (
        f"**{rec}** — {summary}\n\n"
        f"**A. Mathematical Justification**\n{math_just}\n\n"
        f"**B. Real-World Context**\n{ctx_valid}\n\n"
        f"**Risk Factors**\n{risk}"
    )

    return {
        "analysis":          formatted,
        "confidence_score":  confidence,
        "kelly_pct":         kelly_frac,
        "true_prob_refined": true_prob_refined,
        "ev_pct_refined":    ev_refined,
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
