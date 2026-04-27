"""
telegram_bot.py — Interactive Telegram bot for the Sports EV Model.

Commands:
    /start                   — welcome message and usage guide
    /game <team>             — full EV report for a team (all markets)
    /game <team> <market>    — filtered to one market type
    /today                   — full daily EV scan (all leagues)
    /help                    — list available commands

Market keywords: moneyline, spread, total, props
Example: /game Lakers props   /game Arsenal moneyline

Usage:
    python telegram_bot.py
"""

import sys
import os
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode, ChatAction

sys.path.insert(0, os.path.dirname(__file__))
from config import LOCAL_TZ, LOG_LEVEL
from telegram_notifier import (
    BOT_TOKEN,
    _ev_emoji, _format_odds, _format_game_time, _league_label,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] bot — %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Market keyword → API market key mapping
# ---------------------------------------------------------------------------

MARKET_KEYWORDS: dict = {
    "moneyline": "h2h",
    "ml":        "h2h",
    "spread":    "spreads",
    "spreads":   "spreads",
    "ats":       "spreads",
    "total":     "totals",
    "totals":    "totals",
    "ou":        "totals",
    "props":     "player_props",
    "prop":      "player_props",
}

MARKET_LABELS: dict = {
    "h2h":          "Moneyline",
    "spreads":      "Spread",
    "totals":       "Total",
    "player_props": "Player Props",
}

ALL_STANDARD_MARKETS = ["h2h", "spreads", "totals"]

# Mirror of main.py LEAGUE_ALIASES — used by /report arg validation
LEAGUE_ALIASES: dict = {
    "nhl":        "icehockey_nhl",
    "nba":        "basketball_nba",
    "mlb":        "baseball_mlb",
    "epl":        "soccer_epl",
    "laliga":     "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "mls":        "soccer_usa_mls",
}


# ---------------------------------------------------------------------------
# Arg parsing: split trailing market keyword from team name
# ---------------------------------------------------------------------------

def _parse_game_args(args: list) -> tuple:
    """
    Split /game args into (team_query, market_key | None).

    The last token is treated as a market keyword if it matches
    MARKET_KEYWORDS; otherwise the whole string is the team query.

    Examples
    --------
    ["Lakers"]            → ("Lakers", None)
    ["Lakers", "props"]   → ("Lakers", "player_props")
    ["Man", "City", "ml"] → ("Man City", "h2h")
    ["Arsenal"]           → ("Arsenal", None)
    """
    if not args:
        return "", None

    last = args[-1].lower()
    if last in MARKET_KEYWORDS:
        team = " ".join(args[:-1]).strip()
        market = MARKET_KEYWORDS[last]
        return team, market

    return " ".join(args).strip(), None


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

async def _reply(update: Update, text: str) -> None:
    """Send an HTML-formatted reply, splitting if over Telegram's 4096-char limit."""
    limit = 4096
    while text:
        chunk, text = text[:limit], text[limit:]
        await update.message.reply_text(chunk, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)


def _fetch_all_odds():
    from scripts.odds_fetcher import get_odds_df, SPORT_KEYS
    return get_odds_df(sport_keys=SPORT_KEYS, markets=ALL_STANDARD_MARKETS)


def _fetch_props_ev(sport_key: str, game_id: str, game_meta: dict):
    """
    Fetch player props for one game and run EV on them.
    Returns a DataFrame (may be empty).
    """
    import pandas as pd
    from scripts.odds_fetcher import fetch_player_props
    from models.ev_calculator import find_all_positive_ev

    raw = fetch_player_props(sport_key, game_id)
    if not raw:
        return pd.DataFrame()

    # Normalise to a flat DataFrame
    rows = []
    events = raw if isinstance(raw, list) else [raw]
    for event in events:
        for bookie in event.get("bookmakers", []):
            for mkt in bookie.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    rows.append({
                        "game_id":       game_id,
                        "sport_key":     sport_key,
                        "home_team":     game_meta["home_team"],
                        "away_team":     game_meta["away_team"],
                        "commence_time": game_meta["commence_time"],
                        "bookmaker":     bookie["key"],
                        "market":        mkt["key"],
                        "outcome_name":  outcome.get("description", outcome.get("name", "")),
                        "price":         outcome.get("price"),
                        "point":         outcome.get("point"),
                        "last_update":   mkt.get("last_update"),
                    })

    if not rows:
        return pd.DataFrame()

    props_df = pd.DataFrame(rows)
    props_df["price"] = pd.to_numeric(props_df["price"], errors="coerce")
    props_df["point"] = pd.to_numeric(props_df["point"], errors="coerce")
    props_df["commence_time"] = pd.to_datetime(props_df["commence_time"], utc=True)

    markets_present = props_df["market"].unique().tolist()
    ev_df = find_all_positive_ev(props_df, markets=markets_present, ev_threshold=0.0)
    return ev_df


def _run_ev_for_game(odds_df, game_id: str, markets: list = None):
    """
    Filter odds_df to one game, run the full EV pipeline, apply adjustments.
    Returns a DataFrame sorted by effective_ev_pct descending.
    """
    import pandas as pd
    from models.ev_calculator import find_all_positive_ev
    from models.sport_adjustments import apply_adjustments, GameContext

    markets = markets or ALL_STANDARD_MARKETS
    game_odds = odds_df[odds_df["game_id"] == game_id].copy()
    if game_odds.empty:
        return pd.DataFrame()

    ev_df = find_all_positive_ev(game_odds, markets=markets, ev_threshold=0.0)
    if ev_df.empty:
        return ev_df

    meta = game_odds.iloc[0]
    ctx = GameContext(
        game_id=game_id,
        sport_key=meta["sport_key"],
        home_team=meta["home_team"],
        away_team=meta["away_team"],
    )
    rows = []
    for _, row in ev_df.iterrows():
        try:
            result = apply_adjustments(ctx, [row["true_prob"]], [row["outcome_name"]])
            adj_prob    = result["adjusted_probs"][0]
            conf_mult   = result["confidence_multipliers"][0] if result.get("confidence_multipliers") else 1.0
            flags       = "; ".join(result.get("flags", []))
            warnings    = "; ".join(result.get("warnings", []))
        except Exception:
            adj_prob, conf_mult, flags, warnings = row["true_prob"], 1.0, "", ""

        rows.append({
            **row.to_dict(),
            "adjusted_prob":    round(adj_prob, 4),
            "confidence_mult":  round(conf_mult, 3),
            "adj_flags":        flags,
            "adj_warnings":     warnings,
            "effective_ev_pct": round(row["ev_pct"] * conf_mult, 4),
        })

    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values("effective_ev_pct", ascending=False)
            .reset_index(drop=True))


def _search_teams(odds_df, query: str) -> list:
    from datetime import datetime
    q = query.strip().lower()
    mask = (
        odds_df["home_team"].str.lower().str.contains(q, regex=False) |
        odds_df["away_team"].str.lower().str.contains(q, regex=False)
    )
    all_matches = (
        odds_df[mask][["game_id", "home_team", "away_team", "sport_key", "commence_time"]]
        .drop_duplicates("game_id")
        .sort_values("commence_time")
    )

    # Filter to today's date in CT — always assume "today" unless caller specifies otherwise
    today = datetime.now(LOCAL_TZ).date()
    today_matches = all_matches[
        all_matches["commence_time"].dt.tz_convert(LOCAL_TZ).dt.date == today
    ]

    # Use today's games if any exist, otherwise fall back to all upcoming
    results = today_matches if not today_matches.empty else all_matches
    return results.to_dict("records")


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------

def _format_game_ev_message(
    ev_df,
    game_meta: dict,
    markets_checked: list,
    filter_market: str = None,
    props_df=None,
) -> str:
    """
    Format EV results for a single game.

    Produces a structured Telegram message:
      - Header: game name, league, time
      - +EV Opportunities section grouped by market
      - ❌ line for any checked market that had no +EV bets
      - Summary footer

    Parameters
    ----------
    ev_df : pd.DataFrame | None
        Standard-market EV results (h2h / spreads / totals).
    game_meta : dict
    markets_checked : list
        API market keys that were actually requested.
    filter_market : str | None
        If set, only that market is shown (e.g. "h2h").
    props_df : pd.DataFrame | None
        Player props EV results (separate fetch).
    """
    import pandas as pd

    home   = game_meta["home_team"]
    away   = game_meta["away_team"]
    league = _league_label(game_meta["sport_key"])
    gtime  = _format_game_time(game_meta["commence_time"])
    filter_label = MARKET_LABELS.get(filter_market, "") if filter_market else ""

    lines = [
        f"🔍 <b>Game: {away} vs {home}</b> — {gtime}",
        f"<i>{league}</i>" + (f"  |  <i>Filtered: {filter_label}</i>" if filter_label else ""),
        "",
    ]

    # Check if we have any data at all
    has_standard = ev_df is not None and not ev_df.empty and "effective_ev_pct" in ev_df.columns
    has_props    = props_df is not None and not props_df.empty

    if not has_standard and not has_props:
        lines += [
            "❌ Could not compute EV — insufficient bookmaker data.",
            "<i>Need at least 2 books posting lines for this game.</i>",
        ]
        return "\n".join(lines)

    # Collect +EV rows and no-EV markets for the structured layout
    positive_rows = []   # list of (market_key, row_dict)
    no_ev_markets = []   # market keys that were checked but had 0 +EV bets

    # --- Standard markets ---
    for mkt in markets_checked:
        if mkt == "player_props":
            continue
        if not has_standard:
            no_ev_markets.append(mkt)
            continue
        mkt_rows = ev_df[ev_df["market"] == mkt]
        pos = mkt_rows[mkt_rows["effective_ev_pct"] > 0]
        if pos.empty:
            no_ev_markets.append(mkt)
        else:
            for _, r in pos.iterrows():
                positive_rows.append((mkt, r.to_dict()))

    # --- Props ---
    if "player_props" in markets_checked:
        if has_props:
            pos_props = props_df[props_df.get("ev_pct", props_df.get("effective_ev_pct", 0)) > 0] \
                if "ev_pct" in props_df.columns else pd.DataFrame()
            if pos_props.empty:
                no_ev_markets.append("player_props")
            else:
                for _, r in pos_props.head(5).iterrows():
                    positive_rows.append(("player_props", r.to_dict()))
        else:
            no_ev_markets.append("player_props")

    # --- Build output ---
    if not positive_rows:
        lines.append("❌ <b>No +EV bets found</b> for this game.")
        if filter_market:
            lines.append(f"<i>No edge found on the {filter_label} market.</i>")
        lines.append("")
        # Show top 5 negative-EV lines as context
        if has_standard:
            lines.append("<b>Best available lines (below threshold):</b>")
            lines.append("")
            show = ev_df.head(5)
            for _, row in show.iterrows():
                mkt_label = MARKET_LABELS.get(row.get("market", ""), "")
                ev_pct    = row.get("effective_ev_pct", row.get("ev_pct", 0))
                lines.append(
                    f"⚪ <b>{mkt_label}</b> | {row.get('outcome_name','')}  "
                    f"{_format_odds(row.get('american_odds'))}"
                )
                lines.append(
                    f"   Book: {row.get('bookmaker','')} | EV: {ev_pct:+.1f}%"
                )
    else:
        total_pos = len(positive_rows)
        lines.append(f"💰 <b>+EV Opportunities ({total_pos} bet{'s' if total_pos != 1 else ''}):</b>")
        lines.append("")

        # Sort: positive rows by EV% desc, then render grouped by market
        positive_rows.sort(key=lambda x: x[1].get("effective_ev_pct", x[1].get("ev_pct", 0)), reverse=True)

        prev_mkt = None
        for mkt_key, row in positive_rows:
            # Market sub-header on change
            if mkt_key != prev_mkt:
                if prev_mkt is not None:
                    lines.append("")
                prev_mkt = mkt_key

            mkt_label = MARKET_LABELS.get(mkt_key, mkt_key.title())
            ev_pct    = row.get("effective_ev_pct", row.get("ev_pct", 0))
            emoji     = _ev_emoji(ev_pct)
            outcome   = row.get("outcome_name", "")
            book      = row.get("bookmaker", "")
            odds      = _format_odds(row.get("american_odds") or row.get("price"))
            ev_dol    = row.get("ev", 0)
            true_p    = row.get("true_prob", 0)
            conf      = row.get("confidence_mult", 1.0)
            warn      = " ⚠️" if conf < 1.0 else ""
            flags     = row.get("adj_flags", "") or row.get("adj_warnings", "")
            point     = row.get("point")
            point_str = f" {point:+g}" if point is not None and str(point) != "nan" else ""

            lines.append(f"{emoji} <b>{mkt_label}</b> | {outcome}{point_str}")
            lines.append(f"   Book: {book} ({odds}) | EV: <b>{ev_pct:+.1f}%</b> (${ev_dol:+.2f}){warn}")
            if true_p:
                lines.append(f"   True prob: {true_p:.1%}")
            if flags:
                lines.append(f"   <i>{flags[:70]}</i>")

        # ❌ sections for markets with no +EV
        if no_ev_markets:
            lines.append("")
            for mkt in no_ev_markets:
                lines.append(f"❌ <b>{MARKET_LABELS.get(mkt, mkt.title())}</b> — no +EV found")

    # Footer
    if has_standard and positive_rows:
        all_ev = [r.get("effective_ev_pct", r.get("ev_pct", 0)) for _, r in positive_rows]
        lines += [
            "",
            f"<i>Avg EV: {sum(all_ev)/len(all_ev):.1f}%  |  Max: {max(all_ev):.1f}%</i>",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, (
        "⚡ <b>Sports EV Model Bot</b>\n\n"
        "Just type anything — no commands needed:\n\n"
        "  <code>Lakers</code> — EV report for a team\n"
        "  <code>Lakers props</code> — props only\n"
        "  <code>Arsenal moneyline</code> — moneyline only\n"
        "  <code>NHL spread</code> — all NHL spread +EV bets\n"
        "  <code>NBA totals</code> — all NBA totals\n\n"
        "<b>Commands:</b>\n"
        "  /report mlb — full EV scan for a league\n"
        "  /report nhl spread — league + market filter\n"
        "  /game Lakers props — single team lookup\n"
        "  /today — all leagues scan\n"
        "  /help — show this message\n\n"
        "<b>Leagues:</b> nhl · nba · mlb · epl · laliga · bundesliga · mls\n"
        "<b>Markets:</b> moneyline · spread · total · props"
    ))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /game <team> [market]

    market is optional. If omitted, all markets are shown.
    Valid market keywords: moneyline | ml | spread | ats | total | ou | props | prop
    """
    if not context.args:
        await _reply(update,
            "Usage: /game &lt;team&gt; [market]\n\n"
            "Examples:\n"
            "  /game Lakers\n"
            "  /game Lakers props\n"
            "  /game Arsenal moneyline\n"
            "  /game Bruins spread"
        )
        return

    team_query, filter_market = _parse_game_args(context.args)

    if not team_query:
        await _reply(update, "Please provide a team name.\nExample: /game Lakers props")
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    # 1. Fetch odds (always need standard markets for team search)
    try:
        odds_df = _fetch_all_odds()
    except Exception as exc:
        log.error("/game fetch failed: %s", exc)
        await _reply(update, f"⚠️ Failed to fetch odds data.\n<code>{exc}</code>")
        return

    if odds_df.empty:
        await _reply(update, "⚠️ No odds data available right now. Try again shortly.")
        return

    # 2. Search for team
    matches = _search_teams(odds_df, team_query)

    # 3. No match
    if not matches:
        await _reply(update,
            f"❌ No game found for <b>{team_query}</b> today.\n\n"
            f"<i>Try a partial name — e.g. /game Man for Manchester teams.</i>"
        )
        return

    # 4. Multiple matches
    if len(matches) > 1:
        mkt_hint = f" {context.args[-1]}" if filter_market else ""
        lines = [f"🔍 Multiple games match <b>{team_query}</b> — be more specific:\n"]
        for m in matches[:8]:
            gtime  = _format_game_time(m["commence_time"])
            league = _league_label(m["sport_key"])
            lines.append(f"  • {m['away_team']} @ {m['home_team']}  [{league}, {gtime}]")
        lines.append(
            f"\n<i>Then try: /game &lt;full name&gt;{mkt_hint}</i>"
        )
        await _reply(update, "\n".join(lines))
        return

    # 5. Exactly one match
    game_meta = matches[0]
    await update.message.chat.send_action(ChatAction.TYPING)

    # Determine which markets to run
    if filter_market == "player_props":
        std_markets    = []
        include_props  = True
    elif filter_market:
        std_markets    = [filter_market]
        include_props  = False
    else:
        std_markets    = ALL_STANDARD_MARKETS
        include_props  = False   # props shown as hint, fetched only if explicitly requested

    markets_checked = std_markets + (["player_props"] if include_props else [])

    # 6. Run standard EV
    ev_df = None
    if std_markets:
        try:
            ev_df = _run_ev_for_game(odds_df, game_meta["game_id"], markets=std_markets)
        except Exception as exc:
            log.error("/game EV calc failed: %s", exc)
            await _reply(update, f"⚠️ EV calculation failed.\n<code>{exc}</code>")
            return

    # 7. Run props EV
    props_ev = None
    if include_props:
        await update.message.chat.send_action(ChatAction.TYPING)
        try:
            props_ev = _fetch_props_ev(
                game_meta["sport_key"], game_meta["game_id"], game_meta
            )
        except Exception as exc:
            log.warning("Props fetch failed for %s: %s", game_meta["game_id"], exc)

    # 8. Props availability hint (when not explicitly requested)
    props_note = ""
    if not include_props and not filter_market:
        try:
            from scripts.odds_fetcher import fetch_player_props
            raw = fetch_player_props(game_meta["sport_key"], game_meta["game_id"])
            if raw:
                props_note = (
                    f"\n\n<i>ℹ️ Player props available — "
                    f"use /game {team_query} props to include them.</i>"
                )
        except Exception:
            pass

    # 9. Format and send
    message = _format_game_ev_message(
        ev_df=ev_df,
        game_meta=game_meta,
        markets_checked=markets_checked,
        filter_market=filter_market,
        props_df=props_ev,
    )
    message += props_note

    await _reply(update, message)
    log.info("/game %s [%s] → %s @ %s, ev_rows=%s props_rows=%s",
             team_query, filter_market or "all",
             game_meta["away_team"], game_meta["home_team"],
             len(ev_df) if ev_df is not None else 0,
             len(props_ev) if props_ev is not None else 0)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today — full daily EV scan, same as running main.py --league all --market all."""
    await _reply(update, "⏳ Running full EV scan across all leagues… (this takes ~15s)")
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        from scripts.report_generator import run_pipeline
        from scripts.odds_fetcher import SPORT_KEYS
        from telegram_notifier import notify_pipeline_results

        ev_df = run_pipeline(sport_keys=SPORT_KEYS, markets=ALL_STANDARD_MARKETS)
        notify_pipeline_results(ev_df, title="On-Demand Daily Report")

        count = len(ev_df) if ev_df is not None and not ev_df.empty else 0
        await _reply(update, f"✅ Scan complete — <b>{count} +EV bet(s)</b> sent to chat.")

    except Exception as exc:
        log.error("/today failed: %s", exc)
        await _reply(update, f"⚠️ Scan failed.\n<code>{exc}</code>")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /report [league] [market]

    Run a full EV scan for a specific league and optional market.
    If no league given, scans all leagues (same as /today).

    Examples:
        /report mlb
        /report nhl spread
        /report nba moneyline
        /report        (all leagues)
    """
    from main import resolve_leagues, resolve_markets
    from scripts.report_generator import run_pipeline
    from telegram_notifier import notify_pipeline_results

    raw_args = list(context.args) if context.args else []
    log.info("/report called with args: %s", raw_args)

    # Parse league and market from args
    league_args = []
    market_args = []
    for token in raw_args:
        if token.lower() in LEAGUE_ALIASES:
            league_args.append(token.lower())
        elif token.lower() in MARKET_KEYWORDS:
            market_args.append(token.lower())
        else:
            log.warning("/report — unrecognised token: %r", token)

    # Defaults
    if not league_args:
        league_args = ["all"]
    if not market_args:
        market_args = ["all"]

    log.info("/report resolved — leagues: %s  markets: %s", league_args, market_args)

    # Resolve to API keys
    try:
        sport_keys = resolve_leagues(league_args)
        log.info("/report sport_keys: %s", sport_keys)
    except SystemExit:
        await _reply(update,
            f"❌ Unknown league. Valid options: {', '.join(LEAGUE_ALIASES.keys())}")
        return

    std_markets, _ = resolve_markets(market_args)
    if not std_markets:
        std_markets = ALL_STANDARD_MARKETS
    log.info("/report markets: %s", std_markets)

    league_label = ", ".join(t.upper() for t in league_args) if "all" not in league_args else "All Leagues"
    mkt_label    = ", ".join(t.title() for t in market_args) if "all" not in market_args else "All Markets"

    await _reply(update, f"⏳ Running EV scan — <b>{league_label}</b> / {mkt_label}…")
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        ev_df = run_pipeline(sport_keys=sport_keys, markets=std_markets)
        log.info("/report pipeline complete — %d rows", len(ev_df) if ev_df is not None else 0)

        notify_pipeline_results(ev_df, title=f"{league_label} EV Report")

        count = len(ev_df) if ev_df is not None and not ev_df.empty else 0
        if count == 0:
            await _reply(update,
                f"❌ No +EV bets found for <b>{league_label}</b> / {mkt_label} "
                f"above the 3% threshold right now.")
        else:
            await _reply(update,
                f"✅ <b>{count} +EV bet(s)</b> found for {league_label} — report sent above.")

    except Exception as exc:
        log.error("/report failed: %s", exc, exc_info=True)
        await _reply(update, f"⚠️ Scan failed.\n<code>{exc}</code>")


# ---------------------------------------------------------------------------
# Free-text parsing
# ---------------------------------------------------------------------------

SPORT_KEYWORDS: dict = {
    "nhl":        ["icehockey_nhl"],
    "hockey":     ["icehockey_nhl"],
    "nba":        ["basketball_nba"],
    "basketball": ["basketball_nba"],
    "mlb":        ["baseball_mlb"],
    "baseball":   ["baseball_mlb"],
    "epl":        ["soccer_epl"],
    "premier":    ["soccer_epl"],
    "laliga":     ["soccer_spain_la_liga"],
    "la liga":    ["soccer_spain_la_liga"],
    "bundesliga": ["soccer_germany_bundesliga"],
    "mls":        ["soccer_usa_mls"],
    "soccer":     ["soccer_epl", "soccer_spain_la_liga",
                   "soccer_germany_bundesliga", "soccer_usa_mls"],
}


    # Words that carry no signal and should be stripped before keyword matching
_NOISE_WORDS = {
    "find", "me", "a", "an", "the", "good", "best", "any", "some", "show",
    "give", "get", "what", "whats", "what's", "is", "are", "there", "pick",
    "picks", "bet", "bets", "betting", "wager", "wagers", "play", "plays",
    "for", "today", "tonight", "now", "current", "right", "now", "on",
    "with", "in", "at", "of", "to", "and", "or", "my", "i", "can", "you",
    "should", "would", "could", "recommend", "suggest", "worth", "taking",
    "worth", "placing", "available", "have", "do", "look", "like",
}


def _parse_free_text(text: str) -> tuple:
    """
    Parse a free-form natural-language message into (team_query, filter_market, sport_keys).

    Strips noise words first so conversational queries resolve correctly:
        "find me a good MLB bet for today"  → ("", None, ["baseball_mlb"])
        "any good NBA props tonight?"       → ("", "player_props", ["basketball_nba"])
        "Lakers props"                      → ("Lakers", "player_props", None)
        "NHL spread"                        → ("", "spreads", ["icehockey_nhl"])
        "Arsenal moneyline"                 → ("Arsenal", "h2h", None)
    """
    tokens     = text.strip().split()
    remaining  = []
    market_key = None
    sport_keys = None

    for token in tokens:
        lower = token.lower().rstrip("?!.,")
        if lower in _NOISE_WORDS:
            continue                          # strip filler
        if lower in MARKET_KEYWORDS and market_key is None:
            market_key = MARKET_KEYWORDS[lower]
        elif lower in SPORT_KEYWORDS and sport_keys is None:
            sport_keys = SPORT_KEYWORDS[lower]
        else:
            remaining.append(token)

    team_query = " ".join(remaining).strip()
    return team_query, market_key, sport_keys


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle any plain-text message (not a /command).

    Parses team name, market, and/or sport from the message and routes
    to the same pipeline as /game.
    """
    text = (update.message.text or "").strip()
    if not text:
        return

    team_query, filter_market, sport_keys = _parse_free_text(text)

    # Sport-only (or sport+market) query — pull from the live DB cache instantly
    if not team_query and sport_keys:
        # Fake the args so cmd_picks handles it cleanly
        league_token = next((k for k, v in SPORT_KEYWORDS.items() if v == sport_keys), None)
        market_token = next((k for k, v in MARKET_KEYWORDS.items() if v == filter_market), None) if filter_market else None
        context.args = [t for t in [league_token, market_token] if t]
        await cmd_picks(update, context)
        return

    # No team and no sport — prompt
    if not team_query:
        await _reply(update,
            "👋 Just ask naturally — here are some examples:\n\n"
            "  <code>find me a good MLB bet today</code>\n"
            "  <code>any NHL props tonight?</code>\n"
            "  <code>best NBA picks</code>\n"
            "  <code>Lakers props</code>\n"
            "  <code>Arsenal moneyline</code>\n\n"
            "Or use commands: /picks · /picks mlb · /picks nhl props"
        )
        return

    # Team query — reuse cmd_game logic by faking context.args
    context.args = team_query.split()
    if filter_market:
        # Re-append the original market token so _parse_game_args picks it up
        for alias, key in MARKET_KEYWORDS.items():
            if key == filter_market:
                context.args.append(alias)
                break
    await cmd_game(update, context)


# ---------------------------------------------------------------------------
# Auth guard — only respond to the configured CHAT_ID
# ---------------------------------------------------------------------------

from telegram_notifier import CHAT_ID as _ALLOWED_CHAT_ID

def _is_authorized(update: Update) -> bool:
    """Return True only if the message is from the configured TELEGRAM_CHAT_ID."""
    if not _ALLOWED_CHAT_ID:
        return True   # no restriction configured — allow all (dev mode)
    return str(update.effective_chat.id) == str(_ALLOWED_CHAT_ID)


async def _auth_guard(update: Update) -> bool:
    """Send a rejection and return False if the sender is not authorized."""
    if not _is_authorized(update):
        log.warning("Unauthorised message from chat_id=%s", update.effective_chat.id)
        await update.message.reply_text("⛔ Unauthorised.")
        return False
    return True


# ---------------------------------------------------------------------------
# /picks — instant picks from the live DB cache (no API calls)
# ---------------------------------------------------------------------------

MARKET_DISPLAY = {
    "h2h":           "Moneyline",
    "spreads":       "Spread",
    "totals":        "Total",
    "team_totals":   "Team Total",
    "nrfi":          "NRFI/YRFI",
    "player_points": "Pts", "player_rebounds": "Reb", "player_assists": "Ast",
    "player_shots_on_goal": "SOG", "player_goals": "Goals",
    "batter_home_runs": "HR", "pitcher_strikeouts": "K's",
    "batter_total_bases": "Total Bases", "batter_hits": "Hits",
}

LEAGUE_DISPLAY = {
    "icehockey_nhl": "NHL", "basketball_nba": "NBA", "baseball_mlb": "MLB",
    "soccer_epl": "EPL", "soccer_spain_la_liga": "La Liga",
    "soccer_germany_bundesliga": "Bundesliga", "soccer_usa_mls": "MLS",
    "soccer_uefa_champs_league": "UCL", "americanfootball_nfl": "NFL",
}

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /picks [league] [market]

    Returns the current +EV picks straight from the live dashboard cache —
    instant, no API calls required. Refreshes automatically every 30 min.

    Examples:
        /picks           — all picks sorted by EV%
        /picks nhl       — NHL only
        /picks props     — props only
        /picks mlb props — MLB props only
    """
    if not await _auth_guard(update):
        return

    # Parse optional filters from args
    args = [a.lower() for a in (context.args or [])]
    league_filter = None
    market_filter = None
    for token in args:
        if token in LEAGUE_ALIASES:
            league_filter = LEAGUE_ALIASES[token]
        elif token in MARKET_KEYWORDS:
            market_filter = MARKET_KEYWORDS[token]

    # Query the live cache
    from db.database import SessionLocal, EVBetCache
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        query = db.query(EVBetCache)
        if league_filter:
            query = query.filter(EVBetCache.league == league_filter)
        if market_filter == "player_props":
            query = query.filter(EVBetCache.is_prop.is_(True))
        elif market_filter:
            query = query.filter(EVBetCache.market == market_filter)
        # Only upcoming games
        now = datetime.now(timezone.utc)
        query = query.filter(
            (EVBetCache.commence_time == None) | (EVBetCache.commence_time > now)
        )
        bets = query.order_by(EVBetCache.ev_percent.desc()).limit(25).all()
    finally:
        db.close()

    if not bets:
        league_hint = f" [{league_filter.upper() if league_filter else 'all leagues'}]" if args else ""
        await _reply(update,
            f"📭 No +EV picks in cache right now{league_hint}.\n"
            "The dashboard refreshes every 30 min — try again shortly."
        )
        return

    # Build filter label
    filter_parts = []
    if league_filter:
        filter_parts.append(LEAGUE_DISPLAY.get(league_filter, league_filter.upper()))
    if market_filter:
        filter_parts.append(MARKET_DISPLAY.get(market_filter, market_filter.replace("_", " ").title()))
    filter_label = " · ".join(filter_parts) if filter_parts else "All"

    now_str = datetime.now(timezone.utc).strftime("%-I:%M %p UTC")
    lines = [f"⚡ <b>+EV Picks</b>  [{filter_label}]  <i>as of {now_str}</i>\n"]

    for bet in bets:
        odds_str  = f"+{bet.odds}" if bet.odds > 0 else str(bet.odds)
        league    = LEAGUE_DISPLAY.get(bet.league, bet.league or "")
        market    = MARKET_DISPLAY.get(bet.market, (bet.market or "").replace("_", " ").title())
        book      = bet.book.title() if bet.book else ""
        ev        = f"{bet.ev_percent:.1f}%"
        team      = bet.team or ""
        if bet.point is not None:
            pt = int(bet.point) if bet.point == int(bet.point) else bet.point
            team = f"{team} {'+' if float(bet.point) > 0 else ''}{pt}" if bet.market == "spreads" else f"{team} {pt}"
        player    = f" ({bet.player_name})" if bet.player_name else ""
        game_time = ""
        if bet.commence_time:
            from zoneinfo import ZoneInfo
            ct = bet.commence_time.astimezone(ZoneInfo("America/Chicago"))
            game_time = f"  🕐 {ct.strftime('%-I:%M %p CT')}"

        lines.append(
            f"• <b>{team}</b>{player}  {odds_str}  <b>{ev} EV</b>\n"
            f"  {league} · {market} · {book}{game_time}"
        )

    lines.append(f"\n<i>Showing top {len(bets)} picks. Use /picks nhl, /picks props, etc. to filter.</i>")
    await _reply(update, "\n".join(lines))
    log.info("/picks [%s] → %d picks returned to chat_id=%s", filter_label, len(bets), update.effective_chat.id)


# Apply auth guard to existing interactive commands
_orig_cmd_start = cmd_start
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_guard(update): return
    await _orig_cmd_start(update, context)

_orig_cmd_help = cmd_help
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_guard(update): return
    await _orig_cmd_help(update, context)

_orig_handle_text = handle_text
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _auth_guard(update): return
    await _orig_handle_text(update, context)


# ---------------------------------------------------------------------------
# Bot entrypoint
# ---------------------------------------------------------------------------

def _build_app():
    """Build the Application with all handlers registered."""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("picks",  cmd_picks))
    app.add_handler(CommandHandler("game",   cmd_game))
    app.add_handler(CommandHandler("today",  cmd_today))
    app.add_handler(CommandHandler("report", cmd_report))
    # Free-text handler — registered last so commands take priority
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def run_polling_async() -> None:
    """
    Start polling using the async context-manager API.

    Unlike run_polling(), this does NOT install OS signal handlers, so it is
    safe to call from a background thread (not the main thread).
    """
    import asyncio
    app = _build_app()
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Bot polling started — waiting for messages.")
        await asyncio.sleep(float("inf"))   # run until the thread is killed


def main() -> None:
    """Entry point for running the bot standalone (python telegram_bot.py)."""
    import asyncio
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    log.info("Starting bot (polling)…")
    # run_polling() is fine here — we ARE on the main thread
    _build_app().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
