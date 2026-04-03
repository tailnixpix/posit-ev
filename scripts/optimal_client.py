"""
scripts/optimal_client.py — HTTP client for the Optimal Bet MCP server.

The Optimal server (https://mcp.tangiers.ai/) exposes sports data tools
via the MCP JSON-RPC protocol over HTTP/SSE. This module wraps the
low-level transport into clean, typed Python methods.

Available tools
---------------
get_events          — upcoming events for a sport/league
get_game_odds       — odds across books for a specific game
get_game_player_props — player prop markets for a game
get_player_prop_odds  — odds for a specific player prop
get_player_projections — model projections for a player
get_player_gamelogs  — recent game-by-game stats for a player
get_team_history    — recent results and stats for a team
search_players      — resolve a player name → player_id
search_teams        — resolve a team name → team_id
get_schema          — introspect available leagues / markets
query               — freeform SQL-like query

Usage
-----
    from scripts.optimal_client import OptimalClient
    client = OptimalClient()

    # Find player ID
    players = client.search_players("Luka Doncic")
    player_id = players[0]["player_id"]

    # Get projections
    proj = client.get_player_projections(player_id)
"""

import json
import logging
import subprocess
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

_BASE_URL = "https://mcp.tangiers.ai/"
_HEADERS = [
    "-H", "Accept: application/json, text/event-stream",
    "-H", "Content-Type: application/json",
]
_TIMEOUT = 30   # seconds per curl call
_RPC_ID = 1     # stateless — reuse the same ID each call


# ---------------------------------------------------------------------------
# Low-level transport
# ---------------------------------------------------------------------------

def _rpc(method: str, params: dict) -> Any:
    """
    Send one JSON-RPC 2.0 request to the Optimal MCP server and return
    the parsed result. Returns None on failure.

    The server responds with a Server-Sent Events stream. We find the line
    beginning with ``data: `` and parse the embedded JSON.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": _RPC_ID,
        "method": method,
        "params": params,
    })

    cmd = [
        "curl", "-s", "--max-time", str(_TIMEOUT),
        *_HEADERS,
        "-X", "POST",
        "-d", payload,
        _BASE_URL,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT + 5)
        raw = result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("Optimal MCP: curl timed out for method=%s", method)
        return None
    except Exception as exc:
        log.error("Optimal MCP: subprocess error for method=%s: %s", method, exc)
        return None

    # Parse SSE: find first `data: {...}` line
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_str = line[len("data:"):].strip()
            try:
                rpc_resp = json.loads(json_str)
                if "error" in rpc_resp:
                    log.warning("Optimal MCP error (method=%s): %s", method, rpc_resp["error"])
                    return None
                result_obj = rpc_resp.get("result", {})
                # MCP tool responses embed the data in result.content[0].text
                content = result_obj.get("content", [])
                if content and isinstance(content, list):
                    text = content[0].get("text", "")
                    try:
                        return json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        return text
                return result_obj
            except json.JSONDecodeError as exc:
                log.error("Optimal MCP: JSON parse error for method=%s: %s", method, exc)
                return None

    log.warning("Optimal MCP: no data line in response for method=%s. Raw: %s", method, raw[:200])
    return None


def _call_tool(tool_name: str, arguments: dict) -> Any:
    """Wrap _rpc for MCP tools/call method."""
    return _rpc("tools/call", {"name": tool_name, "arguments": arguments})


# ---------------------------------------------------------------------------
# Public client class
# ---------------------------------------------------------------------------

class OptimalClient:
    """
    Thin wrapper around the Optimal Bet MCP server tools.
    All methods return parsed Python dicts/lists, or None on failure.
    """

    # ── Discovery ──────────────────────────────────────────────────────────

    def get_schema(self) -> Optional[Any]:
        """Return available leagues, markets, and tool schemas."""
        return _call_tool("get_schema", {})

    # ── Events / schedule ─────────────────────────────────────────────────

    def get_events(
        self,
        league: str,
        date: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Return upcoming events for a league.

        Parameters
        ----------
        league : str
            e.g. "NBA", "MLB", "NHL", "NFL"
        date : str, optional
            ISO date string "YYYY-MM-DD". Defaults to today.
        """
        args: dict = {"league": league}
        if date:
            args["date"] = date
        return _call_tool("get_events", args)

    # ── Game odds ─────────────────────────────────────────────────────────

    def get_game_odds(
        self,
        game_id: str,
        market: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Return current odds for a specific game across all books.

        Parameters
        ----------
        game_id : str
            Game identifier from get_events.
        market : str, optional
            e.g. "moneyline", "spread", "total"
        """
        args: dict = {"game_id": game_id}
        if market:
            args["market"] = market
        return _call_tool("get_game_odds", args)

    # ── Player props ───────────────────────────────────────────────────────

    def get_game_player_props(
        self,
        game_id: str,
        prop_type: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Return all player prop markets for a game.

        Parameters
        ----------
        game_id : str
        prop_type : str, optional
            e.g. "points", "rebounds", "assists", "home_runs"
        """
        args: dict = {"game_id": game_id}
        if prop_type:
            args["prop_type"] = prop_type
        return _call_tool("get_game_player_props", args)

    def get_player_prop_odds(
        self,
        game_id: str,
        player_id: str,
        prop_type: str,
    ) -> Optional[Any]:
        """
        Return odds across books for one specific player prop line.

        Parameters
        ----------
        game_id : str
        player_id : str
        prop_type : str
            e.g. "points", "rebounds", "strikeouts"
        """
        return _call_tool("get_player_prop_odds", {
            "game_id": game_id,
            "player_id": player_id,
            "prop_type": prop_type,
        })

    # ── Player data ────────────────────────────────────────────────────────

    def get_player_projections(
        self,
        player_id: str,
        game_id: Optional[str] = None,
    ) -> Optional[Any]:
        """
        Return model projections for a player (optionally game-specific).

        Parameters
        ----------
        player_id : str
        game_id : str, optional
        """
        args: dict = {"player_id": player_id}
        if game_id:
            args["game_id"] = game_id
        return _call_tool("get_player_projections", args)

    def get_player_gamelogs(
        self,
        player_id: str,
        last_n: int = 10,
    ) -> Optional[Any]:
        """
        Return recent game-by-game stats for a player.

        Parameters
        ----------
        player_id : str
        last_n : int
            Number of recent games to return (default 10).
        """
        return _call_tool("get_player_gamelogs", {
            "player_id": player_id,
            "last_n": last_n,
        })

    # ── Team data ──────────────────────────────────────────────────────────

    def get_team_history(
        self,
        team_id: str,
        last_n: int = 10,
    ) -> Optional[Any]:
        """
        Return recent results and stats for a team.

        Parameters
        ----------
        team_id : str
        last_n : int
            Number of recent games (default 10).
        """
        return _call_tool("get_team_history", {
            "team_id": team_id,
            "last_n": last_n,
        })

    # ── Search ─────────────────────────────────────────────────────────────

    def search_players(self, name: str, league: Optional[str] = None) -> Optional[Any]:
        """
        Resolve a player name to a player_id.

        Parameters
        ----------
        name : str
            Full or partial player name.
        league : str, optional
            Narrow search to one league (e.g. "NBA").
        """
        args: dict = {"name": name}
        if league:
            args["league"] = league
        return _call_tool("search_players", args)

    def search_teams(self, name: str, league: Optional[str] = None) -> Optional[Any]:
        """
        Resolve a team name to a team_id.

        Parameters
        ----------
        name : str
        league : str, optional
        """
        args: dict = {"name": name}
        if league:
            args["league"] = league
        return _call_tool("search_teams", args)

    # ── Freeform query ─────────────────────────────────────────────────────

    def query(self, q: str) -> Optional[Any]:
        """
        Freeform natural-language or SQL-like query against the Optimal data model.

        Parameters
        ----------
        q : str
            e.g. "Top 5 NBA players by points per game last 10 games"
        """
        return _call_tool("query", {"query": q})


# ---------------------------------------------------------------------------
# Module-level singleton for import convenience
# ---------------------------------------------------------------------------

_default_client: Optional[OptimalClient] = None


def get_client() -> OptimalClient:
    """Return a shared OptimalClient instance (lazy-initialised)."""
    global _default_client
    if _default_client is None:
        _default_client = OptimalClient()
    return _default_client


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")

    client = OptimalClient()

    print("=== Schema ===")
    schema = client.get_schema()
    print(json.dumps(schema, indent=2)[:500] if schema else "None")

    league = sys.argv[1] if len(sys.argv) > 1 else "NBA"
    print(f"\n=== Events: {league} ===")
    events = client.get_events(league)
    print(json.dumps(events, indent=2)[:1000] if events else "None")

    if events and isinstance(events, list) and events:
        game = events[0]
        gid = game.get("game_id") or game.get("id")
        if gid:
            print(f"\n=== Game Odds: {gid} ===")
            odds = client.get_game_odds(gid)
            print(json.dumps(odds, indent=2)[:1000] if odds else "None")
