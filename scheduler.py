"""
scheduler.py — Automated runner for the Sports EV Model.

Schedules:
  1. Daily at 09:00 local time  — morning odds scan
  2. Daily at 13:00 local time  — afternoon odds scan
  3. Daily at 08:00 local time  — fetches today's slate and dynamically
     schedules a one-time run 2 hours before the first game of the day

The pre-game run uses threading.Timer so it fires at the exact moment
regardless of the schedule library's tick interval.

Usage:
    python scheduler.py                        # run with defaults (all leagues, all markets)
    python scheduler.py --league nhl nba       # restrict leagues
    python scheduler.py --market moneyline     # restrict markets
    python scheduler.py --dry-run              # print schedule without running jobs
    python scheduler.py --run-now              # fire a scan immediately then keep scheduling
"""

import sys
import os
import time
import logging
import argparse
import threading
from datetime import datetime, timedelta, timezone

import schedule

sys.path.insert(0, os.path.dirname(__file__))
from config import LOG_LEVEL, LOCAL_TZ
from telegram_notifier import notify_pipeline_results, send_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] scheduler — %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pre-game timer tracking (cancel stale timers on re-schedule)
# ---------------------------------------------------------------------------

_pregame_timer: threading.Timer = None
_pregame_timer_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Core job: run the EV pipeline
# ---------------------------------------------------------------------------

def run_ev_scan(label: str, league_args: list, market_args: list) -> None:
    """Run the full EV pipeline, save CSV, and send Telegram notifications."""
    log.info("=== Starting EV scan [%s] ===", label)
    try:
        from main import resolve_leagues, resolve_markets
        from scripts.report_generator import run_pipeline, save_csv, print_rich_report

        sport_keys = resolve_leagues(league_args)
        std_markets, _ = resolve_markets(market_args)

        ev_df = run_pipeline(
            sport_keys=sport_keys,
            markets=std_markets,
            apply_adjustments_flag=True,
        )

        # Save CSV
        if not ev_df.empty:
            path = save_csv(ev_df)
            log.info("Saved: %s (%d rows)", path, len(ev_df))

        # Rich terminal output
        print_rich_report(ev_df, sports_scanned=sport_keys, run_ts=datetime.now(timezone.utc))

        # Telegram notification
        title = f"{label.title()} +EV Report"
        notify_pipeline_results(ev_df, title=title)

        if ev_df.empty:
            log.info("=== EV scan [%s] completed — no +EV bets above threshold ===", label)
        else:
            log.info("=== EV scan [%s] completed — %d bets, Telegram notified ===", label, len(ev_df))

    except Exception as exc:
        log.error("EV scan [%s] failed: %s", label, exc, exc_info=True)
        send_message(f"⚠️ <b>EV scan [{label}] failed</b>\n<code>{exc}</code>")


# ---------------------------------------------------------------------------
# Slate detection: find the earliest game today across all configured leagues
# ---------------------------------------------------------------------------

def fetch_first_game_today(league_args: list) -> "datetime | None":
    """
    Fetch upcoming game times and return the UTC datetime of the first
    game on today's local date, or None if no games are found.
    """
    from main import resolve_leagues, resolve_markets
    from scripts.odds_fetcher import get_odds_df

    sport_keys, _ = resolve_leagues(league_args), None
    try:
        sport_keys = resolve_leagues(league_args)
    except SystemExit:
        sport_keys = ["icehockey_nhl", "basketball_nba",
                      "soccer_epl", "soccer_spain_la_liga",
                      "soccer_germany_bundesliga", "soccer_usa_mls"]

    log.info("Fetching today's slate to determine pre-game run time...")
    try:
        df = get_odds_df(sport_keys=sport_keys, markets=["h2h"])
    except Exception as exc:
        log.error("Failed to fetch slate: %s", exc)
        return None

    if df.empty:
        log.info("No games found for today's slate.")
        return None

    now_local = datetime.now()
    today_date = now_local.date()

    # Filter to games that start today (local time) and haven't started yet
    upcoming = df[["game_id", "commence_time"]].drop_duplicates("game_id").copy()
    upcoming["commence_time"] = upcoming["commence_time"].dt.tz_convert(LOCAL_TZ)
    today_games = upcoming[
        (upcoming["commence_time"].dt.date == today_date) &
        (upcoming["commence_time"] > datetime.now(LOCAL_TZ))
    ]

    if today_games.empty:
        log.info("No upcoming games found for today.")
        return None

    first_game = today_games["commence_time"].min()
    log.info("First game today: %s", first_game.strftime("%Y-%m-%d %H:%M %Z"))
    return first_game.to_pydatetime()


def schedule_pregame_run(league_args: list, market_args: list) -> None:
    """
    Fetch today's slate, compute the pre-game run time (first game − 2h),
    and set a threading.Timer to fire at that moment.

    Safe to call multiple times — cancels any previously pending timer.
    """
    global _pregame_timer

    first_game = fetch_first_game_today(league_args)
    if first_game is None:
        log.info("No pre-game run scheduled (no games found today).")
        return

    pregame_run_time = first_game - timedelta(hours=2)
    now = datetime.now(LOCAL_TZ)

    # Normalise for comparison
    if pregame_run_time.tzinfo is None:
        pregame_run_time = pregame_run_time.astimezone(LOCAL_TZ)

    delay_seconds = (pregame_run_time - now).total_seconds()

    if delay_seconds <= 0:
        log.info(
            "Pre-game run time %s has already passed (first game in <2h). "
            "Firing immediately.",
            pregame_run_time.strftime("%H:%M"),
        )
        delay_seconds = 0

    log.info(
        "Pre-game scan scheduled for %s (%.0f min from now, 2h before first game at %s)",
        pregame_run_time.strftime("%H:%M %Z"),
        delay_seconds / 60,
        first_game.strftime("%H:%M %Z"),
    )

    with _pregame_timer_lock:
        if _pregame_timer is not None and _pregame_timer.is_alive():
            _pregame_timer.cancel()
            log.debug("Cancelled previous pre-game timer.")

        _pregame_timer = threading.Timer(
            delay_seconds,
            run_ev_scan,
            args=["pre-game", league_args, market_args],
        )
        _pregame_timer.daemon = True
        _pregame_timer.start()


# ---------------------------------------------------------------------------
# Schedule setup
# ---------------------------------------------------------------------------

def build_schedule(league_args: list, market_args: list, dry_run: bool = False) -> None:
    """Register all recurring jobs with the schedule library."""

    def morning_job():
        run_ev_scan("09:00 morning", league_args, market_args)

    def afternoon_job():
        run_ev_scan("13:00 afternoon", league_args, market_args)

    def slate_detection_job():
        schedule_pregame_run(league_args, market_args)

    # Fixed daily scans
    schedule.every().day.at("09:00").do(morning_job)
    schedule.every().day.at("13:00").do(afternoon_job)

    # Slate detection: runs at 08:00 each day to find first game and set the timer
    schedule.every().day.at("08:00").do(slate_detection_job)

    if dry_run:
        print("\n[DRY RUN] Registered jobs:")
        for job in schedule.jobs:
            print(f"  {job}")
        print()
        return

    log.info("Scheduler started. Jobs registered:")
    log.info("  09:00 daily  — morning EV scan")
    log.info("  13:00 daily  — afternoon EV scan")
    log.info("  08:00 daily  — slate detection → pre-game timer (first game − 2h)")
    log.info("Leagues : %s", league_args)
    log.info("Markets : %s", market_args)
    log.info("Press Ctrl+C to stop.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scheduler for the Sports EV Model.",
    )
    parser.add_argument(
        "--league", "-l",
        nargs="+",
        default=["all"],
        metavar="LEAGUE",
        help="League(s): nhl nba epl laliga bundesliga mls  or 'all' (default)",
    )
    parser.add_argument(
        "--market", "-m",
        nargs="+",
        default=["all"],
        metavar="MARKET",
        help="Market(s): moneyline spread total  or 'all' (default)",
    )
    parser.add_argument(
        "--morning-time",
        default="09:00",
        metavar="HH:MM",
        help="Local time for morning scan (default: 09:00)",
    )
    parser.add_argument(
        "--afternoon-time",
        default="13:00",
        metavar="HH:MM",
        help="Local time for afternoon scan (default: 13:00)",
    )
    parser.add_argument(
        "--slate-detect-time",
        default="08:00",
        metavar="HH:MM",
        help="Local time to detect today's slate and set pre-game timer (default: 08:00)",
    )
    parser.add_argument(
        "--pregame-hours",
        type=float,
        default=2.0,
        metavar="HOURS",
        help="Hours before first game to run the pre-game scan (default: 2.0)",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Fire an immediate scan on startup before entering the schedule loop",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the schedule and exit without running any jobs",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # Override defaults with any custom times
    build_schedule(
        league_args=args.league,
        market_args=args.market,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0

    # Optional immediate scan on startup
    if args.run_now:
        log.info("--run-now: firing immediate scan before entering schedule loop.")
        run_ev_scan("startup", args.league, args.market)

    # Also attempt to schedule today's pre-game run immediately on startup
    # (covers the case where the scheduler starts after 08:00)
    schedule_pregame_run(args.league, args.market)

    # Main loop
    try:
        while True:
            schedule.run_pending()
            time.sleep(30)   # check every 30 seconds
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user.")
        with _pregame_timer_lock:
            if _pregame_timer is not None and _pregame_timer.is_alive():
                _pregame_timer.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
