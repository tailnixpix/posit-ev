"""
telegram_notifier.py — Send EV reports and alerts to a Telegram chat.

Exposes three synchronous functions (safe to call from any context):
    send_message(text)          — plain text message
    send_ev_report(df)          — formatted top-10 +EV summary
    send_alert(bet_dict)        — immediate single-bet high-value alert

All functions are synchronous wrappers around async Telegram calls so they
integrate cleanly with the scheduler and main pipeline without requiring
the caller to manage an event loop.

Environment variables (loaded from .env):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import asyncio
import logging
import time
from datetime import datetime

from config import LOCAL_TZ

import pandas as pd
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import (
    TelegramError,
    NetworkError,
    RetryAfter,
    TimedOut,
)
from telegram.constants import ParseMode, MessageLimit

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_RETRIES   = 3
RETRY_DELAY   = 5      # seconds between retries (doubled each attempt)
HIGH_EV_THRESHOLD = 8  # EV% that triggers send_alert

if not BOT_TOKEN:
    log.warning("TELEGRAM_BOT_TOKEN not set — Telegram notifications disabled.")
if not CHAT_ID:
    log.warning("TELEGRAM_CHAT_ID not set — Telegram notifications disabled.")


# ---------------------------------------------------------------------------
# Internal async sender with retry logic
# ---------------------------------------------------------------------------

async def _send_async(text: str, parse_mode: str = ParseMode.HTML) -> bool:
    """
    Core async send with exponential back-off retry.

    Returns True on success, False after all retries exhausted.
    """
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Cannot send — BOT_TOKEN or CHAT_ID missing.")
        return False

    # Telegram hard limit: 4096 chars per message
    chunks = _split_message(text, MessageLimit.MAX_TEXT_LENGTH)

    async with Bot(token=BOT_TOKEN) as bot:
        for chunk in chunks:
            delay = RETRY_DELAY
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text=chunk,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True,
                    )
                    break  # success

                except RetryAfter as exc:
                    wait = exc.retry_after + 1
                    log.warning("Telegram rate limit — waiting %ds (attempt %d/%d)", wait, attempt, MAX_RETRIES)
                    await asyncio.sleep(wait)

                except TimedOut:
                    log.warning("Telegram timed out (attempt %d/%d)", attempt, MAX_RETRIES)
                    await asyncio.sleep(delay)
                    delay *= 2

                except NetworkError as exc:
                    log.warning("Telegram network error (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)
                    await asyncio.sleep(delay)
                    delay *= 2

                except TelegramError as exc:
                    log.error("Telegram API error (non-retryable): %s", exc)
                    return False
            else:
                log.error("All %d send attempts failed for a message chunk.", MAX_RETRIES)
                return False

    return True


def _split_message(text: str, limit: int) -> list:
    """Split text into chunks that respect the Telegram message length limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def _run(coro) -> bool:
    """Run an async coroutine synchronously, compatible with Python 3.9."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Inside an existing event loop (e.g. Jupyter) — use a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _ev_emoji(ev_pct: float) -> str:
    if ev_pct > 8:
        return "🔥"
    if ev_pct > 5:
        return "🟢"
    return "🟡"


def _format_odds(odds) -> str:
    try:
        n = int(odds)
        return f"+{n}" if n > 0 else str(n)
    except (TypeError, ValueError):
        return str(odds)


def _format_game_time(ts) -> str:
    try:
        if hasattr(ts, "strftime"):
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.astimezone(LOCAL_TZ)
            return ts.strftime("%a %b %-d %-I:%M%p CT")
        return str(ts)
    except Exception:
        return ""


def _league_label(sport_key: str) -> str:
    mapping = {
        "icehockey_nhl":             "NHL",
        "basketball_nba":            "NBA",
        "baseball_mlb":              "MLB",
        "soccer_epl":                "EPL",
        "soccer_spain_la_liga":      "La Liga",
        "soccer_germany_bundesliga": "Bundesliga",
        "soccer_usa_mls":            "MLS",
    }
    return mapping.get(sport_key, sport_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_message(text: str) -> bool:
    """
    Send a plain-text (HTML-escaped) message to the configured chat.

    Parameters
    ----------
    text : str

    Returns
    -------
    bool — True if sent successfully.

    Example
    -------
    >>> send_message("🚀 EV Model is live!")
    """
    log.info("Sending plain message (%d chars)", len(text))
    return _run(_send_async(text, parse_mode=ParseMode.HTML))


def send_ev_report(df: pd.DataFrame, title: str = "Daily +EV Report") -> bool:
    """
    Format the top 10 +EV bets from the pipeline DataFrame and send to Telegram.

    Color coding:
        🔥  EV > 8%
        🟢  EV 5–8%
        🟡  EV 3–5%

    Parameters
    ----------
    df : pd.DataFrame
        Output of run_pipeline() — must contain columns:
        game, sport_key, market, outcome_name, bookmaker,
        american_odds, true_prob, ev_pct, commence_time.
    title : str

    Returns
    -------
    bool — True if sent successfully.
    """
    if df is None or df.empty:
        return send_message("📭 <b>No +EV bets found today.</b>")

    ev_col = "effective_ev_pct" if "effective_ev_pct" in df.columns else "ev_pct"
    top = df.sort_values(ev_col, ascending=False).head(10)

    now_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M CT")
    lines = [
        f"<b>⚡ {title}</b>",
        f"<i>{now_str} — {len(df)} total bets found, showing top {len(top)}</i>",
        "",
    ]

    prev_league = None
    for _, row in top.iterrows():
        league = _league_label(row.get("sport_key", ""))
        if league != prev_league:
            lines.append(f"<b>── {league} ──</b>")
            prev_league = league

        ev_pct  = row.get(ev_col, 0)
        emoji   = _ev_emoji(ev_pct)
        game    = row.get("game", "Unknown game")
        market  = row.get("market", "")
        outcome = row.get("outcome_name", "")
        book    = row.get("bookmaker", "")
        odds    = _format_odds(row.get("american_odds"))
        prob    = row.get("true_prob", 0)
        ev_dol  = row.get("ev", 0)
        gtime   = _format_game_time(row.get("commence_time"))
        flags   = row.get("adj_flags", "") or row.get("adj_warnings", "")
        warning = "  ⚠️" if row.get("confidence_mult", 1.0) < 1.0 else ""

        lines += [
            f"{emoji} <b>{outcome}</b> {odds} — {book}",
            f"   {game}  |  {market}",
            f"   EV: <b>{ev_pct:.1f}%</b> (${ev_dol:.2f}/unit)  |  True prob: {prob:.1%}{warning}",
            f"   🕐 {gtime}",
        ]
        if flags:
            lines.append(f"   <i>{flags[:80]}</i>")
        lines.append("")

    # Summary footer
    avg_ev = df[ev_col].mean()
    max_ev = df[ev_col].max()
    total_ev = df["ev"].sum() if "ev" in df.columns else 0
    lines += [
        "──────────────────",
        f"📊 Avg EV: <b>{avg_ev:.1f}%</b>  |  Max: <b>{max_ev:.1f}%</b>  |  Total EV$: <b>${total_ev:.2f}</b>",
        f"<i>Stake $100/bet. Past EV does not guarantee future profit.</i>",
    ]

    message = "\n".join(lines)
    log.info("Sending EV report (%d bets, %d chars)", len(top), len(message))
    return _run(_send_async(message))


def send_alert(bet_dict: dict) -> bool:
    """
    Send an immediate single-bet alert for a high-value +EV opportunity.
    Intended to be called when a bet with EV > HIGH_EV_THRESHOLD is found.

    Parameters
    ----------
    bet_dict : dict
        A single row from the EV DataFrame (as a dict), or any dict with keys:
        game, sport_key, market, outcome_name, bookmaker,
        american_odds, true_prob, ev_pct, commence_time.

    Returns
    -------
    bool — True if sent successfully.

    Example
    -------
    >>> send_alert({
    ...     "game": "Bruins @ Leafs",
    ...     "sport_key": "icehockey_nhl",
    ...     "market": "h2h",
    ...     "outcome_name": "Boston Bruins",
    ...     "bookmaker": "draftkings",
    ...     "american_odds": 145,
    ...     "true_prob": 0.44,
    ...     "ev_pct": 9.3,
    ...     "commence_time": "Fri Mar 28 7:00PM",
    ... })
    """
    ev_col  = "effective_ev_pct" if "effective_ev_pct" in bet_dict else "ev_pct"
    ev_pct  = bet_dict.get(ev_col, bet_dict.get("ev_pct", 0))
    emoji   = _ev_emoji(ev_pct)
    league  = _league_label(bet_dict.get("sport_key", ""))
    game    = bet_dict.get("game", "Unknown game")
    market  = bet_dict.get("market", "")
    outcome = bet_dict.get("outcome_name", "")
    book    = bet_dict.get("bookmaker", "")
    odds    = _format_odds(bet_dict.get("american_odds"))
    prob    = bet_dict.get("true_prob", 0)
    ev_dol  = bet_dict.get("ev", 0)
    gtime   = _format_game_time(bet_dict.get("commence_time"))
    warning = "  ⚠️ Confidence penalty applied" if bet_dict.get("confidence_mult", 1.0) < 1.0 else ""

    message = "\n".join([
        f"{emoji} <b>HIGH-VALUE +EV ALERT</b> {emoji}",
        "",
        f"<b>{league}  |  {market.upper()}</b>",
        f"🎯 <b>{outcome}</b>  {odds}  —  {book}",
        f"📋 {game}",
        f"🕐 {gtime}",
        "",
        f"EV:        <b>{ev_pct:.1f}%</b>  (${ev_dol:.2f} per $100){warning}",
        f"True prob: <b>{prob:.1%}</b>",
        f"Implied:   {1 / (1 + (abs(int(odds.replace('+',''))) / 100)) if odds else 'N/A'}",
        "",
        f"<i>Stake $100/bet. Not financial advice.</i>",
    ])

    log.info("Sending high-value alert: %s %s EV=%.1f%%", outcome, odds, ev_pct)
    return _run(_send_async(message))


def notify_pipeline_results(df: pd.DataFrame, title: str = "Daily +EV Report") -> None:
    """
    Convenience function called by the scheduler after each pipeline run:
      1. Sends the full EV report summary.
      2. Fires individual alerts for any bets with EV > HIGH_EV_THRESHOLD.

    Parameters
    ----------
    df : pd.DataFrame
        Output of run_pipeline().
    """
    send_ev_report(df, title=title)

    if df is None or df.empty:
        return

    ev_col = "effective_ev_pct" if "effective_ev_pct" in df.columns else "ev_pct"
    high_ev = df[df[ev_col] > HIGH_EV_THRESHOLD]
    for _, row in high_ev.iterrows():
        send_alert(row.to_dict())
        time.sleep(0.5)   # avoid hitting Telegram's 30 msg/sec limit


# ---------------------------------------------------------------------------
# CLI — quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Telegram notifier")
    parser.add_argument("--test-message", action="store_true", help="Send a test plain message")
    parser.add_argument("--test-alert", action="store_true", help="Send a test high-value alert")
    parser.add_argument("--test-report", action="store_true", help="Send a test EV report with dummy data")
    args = parser.parse_args()

    if args.test_message:
        ok = send_message("🤖 <b>Sports EV Model</b> — Telegram connection test ✅")
        print("Message sent:", ok)

    if args.test_alert:
        ok = send_alert({
            "game": "Boston Bruins @ Toronto Maple Leafs",
            "sport_key": "icehockey_nhl",
            "market": "h2h",
            "outcome_name": "Boston Bruins",
            "bookmaker": "draftkings",
            "american_odds": 145,
            "true_prob": 0.44,
            "ev_pct": 9.3,
            "ev": 9.30,
            "commence_time": datetime.now(),
            "confidence_mult": 1.0,
        })
        print("Alert sent:", ok)

    if args.test_report:
        dummy = pd.DataFrame([
            {"game": "Bruins @ Maple Leafs", "sport_key": "icehockey_nhl", "market": "h2h",
             "outcome_name": "Boston Bruins", "bookmaker": "draftkings", "american_odds": 145,
             "true_prob": 0.44, "ev_pct": 9.3, "effective_ev_pct": 9.3, "ev": 9.30,
             "commence_time": datetime.now(), "confidence_mult": 1.0, "adj_flags": ""},
            {"game": "Lakers @ Nuggets", "sport_key": "basketball_nba", "market": "spreads",
             "outcome_name": "Denver Nuggets -4.5", "bookmaker": "fanduel", "american_odds": -108,
             "true_prob": 0.54, "ev_pct": 4.7, "effective_ev_pct": 4.7, "ev": 4.70,
             "commence_time": datetime.now(), "confidence_mult": 1.0, "adj_flags": ""},
            {"game": "Arsenal @ Chelsea", "sport_key": "soccer_epl", "market": "h2h",
             "outcome_name": "Arsenal", "bookmaker": "betmgm", "american_odds": 210,
             "true_prob": 0.34, "ev_pct": 3.4, "effective_ev_pct": 3.4, "ev": 3.40,
             "commence_time": datetime.now(), "confidence_mult": 0.8, "adj_flags": "AWAY_EURO_FATIGUE"},
        ])
        ok = send_ev_report(dummy, title="Test EV Report")
        print("Report sent:", ok)

    if not any([args.test_message, args.test_alert, args.test_report]):
        parser.print_help()
