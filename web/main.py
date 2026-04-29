"""
web/main.py — FastAPI web application for Posit+EV.

HTML page routes:
    GET  /                    Landing page
    GET  /register            Registration form
    GET  /pricing             Pricing / subscription tiers
    GET  /login               Login form
    GET  /dashboard           Protected: valid JWT + active subscription required
    POST /admin/refresh-cache Manual pipeline trigger (auth required)

Auth routes (handled by web/auth.py router):
    POST /register    Create account → Stripe customer → JWT cookie → /pricing
    POST /login       Verify credentials → JWT cookie → /dashboard
    POST /logout      Clear JWT cookie → /

EV Cache:
    refresh_ev_cache() runs the full pipeline every 30 minutes via APScheduler
    (AsyncIOScheduler). On startup it runs immediately, then again every 30 min.
    Props are skipped for sports that returned no game-level odds in the same run.
    Results are written atomically to the EVBetCache table, replacing all prior rows.
    /dashboard reads directly from EVBetCache — no live API calls on page load.

Run:
    uvicorn web.main:app --reload
"""

import logging
import math
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe as _stripe
import sentry_sdk
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Path setup — allow imports from project root
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db.database import DailyPick, EVBetCache, NewsletterSubscriber, OddsHistory, SessionLocal, User, create_tables  # noqa: E402
from web.auth import (                                                   # noqa: E402
    router as auth_router,
    create_access_token,
    decode_access_token,
    get_db,
    get_token_from_request,
    require_auth,
    setup_exception_handlers,
)
from web.newsletter import (                                             # noqa: E402
    router as newsletter_router,
    send_daily_newsletter,
    send_correction_newsletter,
)
from web.stripe_webhook import router as stripe_router                   # noqa: E402
from web.beehiiv import bulk_sync as bh_bulk_sync, remove_subscriber as bh_remove  # noqa: E402

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentry — error monitoring (no-op if SENTRY_DSN is not set)
# ---------------------------------------------------------------------------

_sentry_dsn = os.getenv("SENTRY_DSN", "")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        traces_sample_rate=0.05,   # 5% of requests for performance tracing
        send_default_pii=False,
    )
    log.info("Sentry initialised.")

# ---------------------------------------------------------------------------
# Admin auth — HTTP Basic (credentials never appear in URLs or logs)
# ---------------------------------------------------------------------------

def _is_admin(request: Request) -> bool:
    """Return True if the current session has a valid admin PIN login."""
    return bool(request.session.get("admin_authenticated"))


# ---------------------------------------------------------------------------
# Projection cache — in-memory, TTL 30 min
# Keyed by (game_str, league) so the same game hit by multiple bets reuses
# the cached result.  Cleared automatically when entries are stale.
# ---------------------------------------------------------------------------

import time as _time

_PROJ_CACHE: dict = {}       # key → {"result": dict, "ts": float}
_PROJ_CACHE_TTL = 1800       # 30 minutes


def _proj_cache_get(key: str) -> Optional[dict]:
    entry = _PROJ_CACHE.get(key)
    if entry and (_time.monotonic() - entry["ts"]) < _PROJ_CACHE_TTL:
        return entry["result"]
    if key in _PROJ_CACHE:
        del _PROJ_CACHE[key]
    return None


def _proj_cache_set(key: str, result: dict) -> None:
    _PROJ_CACHE[key] = {"result": result, "ts": _time.monotonic()}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Posit+EV", docs_url=None, redoc_url=None)

setup_exception_handlers(app)

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(_WEB_DIR, "static")),
    name="static",
)
app.include_router(auth_router)
app.include_router(newsletter_router)
app.include_router(stripe_router)

templates = Jinja2Templates(directory=os.path.join(_WEB_DIR, "templates"))


# ---------------------------------------------------------------------------
# Support contact
# ---------------------------------------------------------------------------

@app.post("/contact/support")
async def contact_support(
    name: str = Form(""),
    email: str = Form(...),
    message: str = Form(...),
):
    """Forward a user support message to support.positev@gmail.com via Resend."""
    import resend as _resend
    _resend.api_key = os.getenv("RESEND_API_KEY", "")

    safe_name  = name.strip() or "(not provided)"
    safe_email = email.strip()
    safe_msg   = message.strip()

    subject = f"[Posit+EV Support] Message from {safe_name}"
    html_body = (
        "<h2 style='font-family:sans-serif;'>Posit+EV Support Request</h2>"
        f"<p style='font-family:sans-serif;'><strong>Name:</strong> {safe_name}</p>"
        f"<p style='font-family:sans-serif;'><strong>Email:</strong> {safe_email}</p>"
        "<hr/>"
        "<p style='font-family:sans-serif;'><strong>Message:</strong></p>"
        "<blockquote style='font-family:sans-serif; border-left:3px solid #534AB7;"
        " margin:0; padding:8px 16px; color:#374151;'>"
        + safe_msg.replace("\n", "<br>") +
        "</blockquote>"
    )

    try:
        _resend.Emails.send({
            "from":     "Posit+EV <noreply@posit-ev.com>",
            "to":       ["support.positev@gmail.com"],
            "reply_to": safe_email,
            "subject":  subject,
            "html":     html_body,
        })
        log.info("Support message forwarded from %s", safe_email)
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        log.error("Support contact email failed: %s", exc)
        return JSONResponse({"status": "error"}, status_code=500)

# ---------------------------------------------------------------------------
# Stripe subscription auto-heal
# ---------------------------------------------------------------------------

def sync_stripe_subscriptions() -> None:
    """
    Hourly job: find users who have a Stripe customer ID but no recorded
    subscription (is_subscribed=False, stripe_subscription_id=None).  Query
    Stripe for each and heal the DB when an active/trialing subscription is
    found.

    This is a safety net for the race where the /success redirect or the
    Stripe webhook fails to update the DB after a successful checkout.
    Scoped to users created within the last 30 days so we don't hammer
    the Stripe API on large user bases.
    """
    import stripe as _stripe_sync
    _stripe_sync.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not _stripe_sync.api_key:
        return

    from datetime import timedelta as _td
    cutoff = datetime.now(timezone.utc) - _td(days=30)

    db = SessionLocal()
    try:
        candidates = (
            db.query(User)
            .filter(
                User.stripe_customer_id.isnot(None),
                User.is_subscribed.is_(False),
                User.stripe_subscription_id.is_(None),
                User.created_at > cutoff,
            )
            .all()
        )
        if not candidates:
            return

        log.info("stripe_sync: checking %d unsynced user(s).", len(candidates))
        healed = 0
        for user in candidates:
            try:
                subs = _stripe_sync.Subscription.list(
                    customer=user.stripe_customer_id,
                    status="all",
                    limit=5,
                )
                active = next(
                    (s for s in subs.auto_paging_iter()
                     if s.status in ("active", "trialing", "past_due")),
                    None,
                )
                if not active:
                    continue

                trial_end_ts = getattr(active, "trial_end", None)
                trial_ends_at = (
                    datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
                    if trial_end_ts else None
                )
                user.is_subscribed          = True
                user.stripe_subscription_id = active.id
                if trial_ends_at:
                    user.trial_ends_at = trial_ends_at
                db.commit()
                log.info(
                    "stripe_sync: healed %s → sub=%s status=%s trial_ends=%s",
                    user.email, active.id, active.status, trial_ends_at,
                )
                healed += 1
            except Exception as exc:
                db.rollback()
                log.warning("stripe_sync: error checking %s: %s", user.email, exc)

        if healed:
            log.info("stripe_sync: healed %d user(s).", healed)
    except Exception as exc:
        log.error("stripe_sync: unexpected error: %s", exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone="America/Chicago")

# ---------------------------------------------------------------------------
# EV cache — pipeline integration
# ---------------------------------------------------------------------------

# In-memory status shown on the dashboard header
_cache_status: dict = {
    "last_run":   None,   # datetime (CT) of last completed refresh
    "last_count": 0,      # number of bets written
    "last_error": None,   # error message string, or None if last run succeeded
    "running":    False,  # True while a refresh is in progress
}


def refresh_ev_cache() -> int:
    """
    Run the full EV pipeline and atomically replace the EVBetCache table.

    Steps
    -----
    1. Import and call run_pipeline() (odds fetch → EV calc → sport adjustments).
    2. Open a DB session, delete all existing EVBetCache rows.
    3. Bulk-insert new rows from the pipeline DataFrame.
    4. Commit. Update _cache_status.

    Returns the number of bets written (0 on error or empty result).

    This is a *synchronous* function — APScheduler's AsyncIOScheduler runs it
    in a thread-pool executor so it never blocks the event loop.
    """
    global _cache_status

    if _cache_status["running"]:
        log.warning("EV cache refresh already in progress — skipping.")
        return 0

    _cache_status["running"] = True
    log.info("EV cache refresh: starting pipeline...")

    try:
        # Lazy import so the web process doesn't pay the pandas/requests import
        # cost at startup — only on the first scheduled run.
        from scripts.report_generator import run_pipeline
        from scripts.odds_fetcher import (
            get_props_df, get_futures_df,
            get_quota_state, reset_quota_state,
            LOW_CREDIT_THRESHOLD, CRITICAL_CREDIT_THRESHOLD,
        )
        from models.ev_calculator import find_positive_ev_props, find_all_positive_ev
        import pandas as _pd

        # Reset exhausted flag so a renewed key works on the very next run
        reset_quota_state()

        ev_df = run_pipeline()

        # ── API quota check — alert immediately if credits ran out ────────
        _quota = get_quota_state()
        if _quota["exhausted"]:
            _quota_msg = (
                "🚨 *Odds API credits exhausted* — bet cards are paused.\n"
                "Renew at https://the-odds-api.com to restore the feed."
            )
            log.critical("EV cache: Odds API quota exhausted — notifying via Telegram.")
            try:
                from telegram_notifier import send_message as _tg_send
                import asyncio as _asyncio
                _asyncio.run(_tg_send(_quota_msg))
            except Exception as _tg_exc:
                log.warning("Telegram quota alert failed: %s", _tg_exc)
            _cache_status.update({
                "running": False, "last_count": 0,
                "last_error": "OUT_OF_USAGE_CREDITS",
                "last_run": datetime.now(timezone.utc),
            })
            return 0

        # ── Low credit warning ────────────────────────────────────────────
        _remaining = _quota.get("remaining")
        if _remaining is not None:
            if _remaining <= CRITICAL_CREDIT_THRESHOLD:
                log.critical("Odds API credits critically low: %d remaining.", _remaining)
                try:
                    from telegram_notifier import send_message as _tg_send
                    import asyncio as _asyncio
                    _asyncio.run(_tg_send(
                        f"🔴 *Odds API: only {_remaining} credits left!* "
                        f"Renew soon at https://the-odds-api.com"
                    ))
                except Exception:
                    pass
            elif _remaining <= LOW_CREDIT_THRESHOLD:
                log.warning("Odds API credits low: %d remaining.", _remaining)
                try:
                    from telegram_notifier import send_message as _tg_send
                    import asyncio as _asyncio
                    _asyncio.run(_tg_send(
                        f"⚠️ *Odds API: {_remaining} credits remaining.* "
                        f"Consider topping up at https://the-odds-api.com"
                    ))
                except Exception:
                    pass

        # ── Player props (NBA, MLB, NHL) ──────────────────────────────────
        # Always attempt props for all configured prop sports.
        # get_props_df() does its own h2h check per sport and returns empty rows
        # for any sport with no upcoming games — no need to pre-filter here.
        # Filtering by ev_df (positive EV game bets) was a bug: it silently
        # skipped prop fetches whenever there were no +EV game-level bets for
        # a sport, even though props are a completely independent market.
        from scripts.odds_fetcher import PROP_SPORTS as _PROP_SPORTS
        log.info("Props fetch: attempting all prop sports: %s", _PROP_SPORTS)
        try:
            props_df = get_props_df(sport_keys=_PROP_SPORTS)
            if not props_df.empty:
                props_ev_df = find_positive_ev_props(props_df)
                if not props_ev_df.empty:
                    ev_df = _pd.concat([ev_df, props_ev_df], ignore_index=True)
                    log.info("Props: found %d +EV prop bets.", len(props_ev_df))
                else:
                    log.info("Props: no +EV prop bets found (threshold not met).")
            else:
                log.info("Props: no prop data returned (no games in window or API issue).")
        except Exception as _props_exc:
            log.warning("Props fetch/calc failed (non-fatal): %s", _props_exc)

        # ── Championship futures (NHL/NBA playoffs) ───────────────────────
        # Uses separate *_championship_winner sport keys with outrights market.
        # No 7-day filter — futures have commence_times months away.
        try:
            futures_df = get_futures_df()
            if not futures_df.empty:
                futures_ev_df = find_all_positive_ev(futures_df, markets=["outrights"])
                if not futures_ev_df.empty:
                    # Tag as non-prop game bets so they pass the newsletter filter
                    futures_ev_df["is_prop"] = False
                    ev_df = _pd.concat([ev_df, futures_ev_df], ignore_index=True)
                    log.info("Futures: found %d +EV championship winner bets.", len(futures_ev_df))
        except Exception as _fut_exc:
            log.warning("Futures fetch/calc failed (non-fatal): %s", _fut_exc)
    except Exception as exc:
        log.error("EV cache refresh: pipeline failed: %s", exc, exc_info=True)
        _cache_status.update({"running": False, "last_error": str(exc),
                               "last_run": datetime.now(timezone.utc)})
        return 0

    db: Session = SessionLocal()
    try:
        # ── Step 1: delete all stale rows and commit immediately ──────────────
        # Committing the delete in its own transaction ensures that even if the
        # subsequent insert fails and rolls back, the old stale rows are gone.
        # Without this split, a failed insert rolls back the delete too and stale
        # rows survive indefinitely.
        deleted = db.query(EVBetCache).delete()
        db.commit()
        log.info("EV cache: cleared %d stale rows (committed).", deleted)

        if ev_df.empty:
            log.info("EV cache refresh: no +EV bets found. Cache cleared.")
            _cache_status.update({
                "running": False, "last_count": 0, "last_error": None,
                "last_run": datetime.now(timezone.utc),
            })
            return 0

        # ── Helper: convert American odds → implied probability (vig-on) ──────
        def _american_to_implied(odds: int) -> float:
            if odds > 0:
                return 100 / (odds + 100)
            else:
                return abs(odds) / (abs(odds) + 100)

        # ── Handle / sharp money enrichment (Action Network, non-fatal) ─────────
        from scripts.handle_fetcher import fetch_handle_for_game, compute_sharp_score as _sharp_score
        _handle_map: dict = {}   # (game, market, team) → (bet_pct, money_pct)
        try:
            for _, _hrow in ev_df.iterrows():
                _hgame   = str(_hrow.get("game", "") or "")
                _hmkt    = str(_hrow.get("market", "") or "")
                _hteam   = str(_hrow.get("outcome_name", "") or "")
                _hleague = str(_hrow.get("sport_key", "") or "")
                _hkey    = (_hgame, _hmkt, _hteam)
                if _hkey not in _handle_map and _hgame and " @ " in _hgame:
                    _bp, _mp = fetch_handle_for_game(
                        game=_hgame, market=_hmkt, team=_hteam, league=_hleague
                    )
                    _handle_map[_hkey] = (_bp, _mp)
            log.info("Handle fetch: enriched %d unique lines.", sum(1 for v in _handle_map.values() if v[0] is not None))
        except Exception as _he:
            log.warning("Handle fetch failed (non-fatal): %s", _he)

        # ── Helper: look up first recorded odds for this bet from OddsHistory ─
        def _get_opening_odds(d_session: Session, game_id: str, book: str, market: str, team: str):
            row_h = (
                d_session.query(OddsHistory.odds)
                .filter(
                    OddsHistory.game_id == game_id,
                    OddsHistory.book    == book,
                    OddsHistory.market  == market,
                    OddsHistory.team    == team,
                )
                .order_by(OddsHistory.captured_at.asc())
                .first()
            )
            return row_h[0] if row_h else None

        # ── Snapshot current +EV bets into OddsHistory (append-only) ─────────
        now_utc = datetime.now(timezone.utc)
        history_rows = []
        for _, row in ev_df.iterrows():
            try:
                h_odds = int(row.get("american_odds", 0))
            except (ValueError, TypeError):
                h_odds = 0

            h_point = row.get("point")
            try:
                h_point = float(h_point) if h_point is not None else None
            except (ValueError, TypeError):
                h_point = None

            h_ct = None
            h_ct_raw = row.get("commence_time")
            if h_ct_raw is not None:
                try:
                    import pandas as pd
                    ts = pd.Timestamp(h_ct_raw)
                    if ts.tzinfo is None:
                        ts = ts.tz_localize("UTC")
                    h_ct = ts.to_pydatetime()
                except Exception:
                    h_ct = None

            h_implied = _american_to_implied(h_odds) if h_odds else None
            history_rows.append(OddsHistory(
                game_id      = str(row.get("game_id", "")),
                league       = str(row.get("sport_key", "")),
                market       = str(row.get("market", "")),
                team         = str(row.get("outcome_name", "")),
                game         = str(row.get("game", "")) or None,
                point        = h_point,
                book         = str(row.get("bookmaker", "")),
                odds         = h_odds,
                implied_prob = h_implied,
                true_prob    = float(row.get("true_prob", 0)) or None,
                ev_percent   = float(row.get("effective_ev_pct", row.get("ev_pct", 0))) or None,
                commence_time = h_ct,
                captured_at  = now_utc,
            ))
        if history_rows:
            db.bulk_save_objects(history_rows)
            db.flush()   # make rows visible for _get_opening_odds queries within this session
            log.info("OddsHistory: appended %d snapshot rows.", len(history_rows))

        rows = []
        for _, row in ev_df.iterrows():
            # Skip rows where EV% is NaN (can happen for futures with missing data)
            _raw_ev = row.get("effective_ev_pct", row.get("ev_pct", 0))
            try:
                _raw_ev_f = float(_raw_ev)
                if _raw_ev_f != _raw_ev_f:  # NaN check
                    continue
            except (TypeError, ValueError):
                continue

            try:
                odds_val = int(row.get("american_odds", 0))
            except (ValueError, TypeError):
                odds_val = 0

            point_val = row.get("point")
            try:
                _pv = float(point_val) if point_val is not None else None
                point_val = None if (_pv is None or _pv != _pv) else _pv  # _pv != _pv catches NaN
            except (ValueError, TypeError):
                point_val = None

            # Parse commence_time — may be a pandas Timestamp or ISO string
            ct_raw = row.get("commence_time")
            ct_val = None
            if ct_raw is not None:
                try:
                    import pandas as pd
                    ts = pd.Timestamp(ct_raw)
                    if ts.tzinfo is None:
                        ts = ts.tz_localize("UTC")
                    ct_val = ts.to_pydatetime()
                except Exception:
                    ct_val = None

            # adj_flags is pipe-separated string from _apply_sport_adjustments
            raw_flags = row.get("adj_flags", "")
            adj_flags_val = str(raw_flags) if raw_flags and str(raw_flags) not in ("nan", "None", "") else None

            # implied_prob: book's vig-inclusive probability from American odds
            implied_prob_val = _american_to_implied(odds_val) if odds_val else None

            # opening_odds: first recorded odds for this game/book/market/team in OddsHistory
            row_game_id = str(row.get("game_id", ""))
            row_book    = str(row.get("bookmaker", ""))
            row_market  = str(row.get("market", ""))
            row_team    = str(row.get("outcome_name", ""))
            opening_odds_val = _get_opening_odds(db, row_game_id, row_book, row_market, row_team)

            # handle / sharp money data
            _hk = (str(row.get("game", "") or ""), row_market, row_team)
            _bet_pct, _money_pct = _handle_map.get(_hk, (None, None))
            _sharp = _sharp_score(_bet_pct, _money_pct, opening_odds_val, odds_val) if (_bet_pct is not None or _money_pct is not None) else None

            # ── Attach projection snapshot (pre-fetched by report_generator) ──
            def _safe_proj_float(val):
                try:
                    v = float(val)
                    return None if (v != v) else v
                except (TypeError, ValueError):
                    return None

            def _safe_proj_str(val):
                s = str(val) if val is not None else ""
                return s if s not in ("", "nan", "None") else None

            cache_row = EVBetCache(
                game_id       = row_game_id or None,
                league        = str(row.get("sport_key",      "")),
                market        = row_market,
                team          = row_team,
                game          = str(row.get("game",           "")) or None,
                point         = point_val,
                commence_time = ct_val,
                book          = row_book,
                source_type   = str(row.get("source_type",    "sportsbook")) or "sportsbook",
                ev_percent    = float(row.get("effective_ev_pct", row.get("ev_pct", 0))),
                true_prob     = float(row.get("true_prob",    0)),
                adjusted_prob = float(row["adjusted_prob"]) if row.get("adjusted_prob") is not None else None,
                adj_flags     = adj_flags_val,
                implied_prob  = implied_prob_val,
                opening_odds  = opening_odds_val,
                odds          = odds_val,
                player_name   = (lambda v: str(v) if v and str(v) not in ("nan", "None", "") else None)(row.get("player_name")),
                is_prop       = row.get("is_prop") is True,
                bet_pct       = _bet_pct,
                money_pct     = _money_pct,
                sharp_score   = _sharp,
                created_at    = datetime.now(timezone.utc),
                # Projection snapshot — passed through from report_generator pipeline
                proj_away_score    = _safe_proj_float(row.get("proj_away_score")),
                proj_home_score    = _safe_proj_float(row.get("proj_home_score")),
                proj_total         = _safe_proj_float(row.get("proj_total")),
                proj_home_win_prob = _safe_proj_float(row.get("proj_home_win_prob")),
                proj_away_display  = _safe_proj_str(row.get("proj_away_display")),
                proj_home_display  = _safe_proj_str(row.get("proj_home_display")),
                home_trend    = _safe_proj_str(row.get("home_trend")),
                away_trend    = _safe_proj_str(row.get("away_trend")),
                all_book_odds = _safe_proj_str(row.get("all_book_odds")),
            )
            rows.append(cache_row)

        db.add_all(rows)
        db.commit()
        count = len(rows)
        log.info("EV cache refresh: wrote %d bets.", count)
        _cache_status.update({
            "running": False, "last_count": count, "last_error": None,
            "last_run": datetime.now(timezone.utc),
        })
        return count

    except Exception as exc:
        db.rollback()
        log.error("EV cache refresh: DB write failed: %s", exc, exc_info=True)
        _cache_status.update({"running": False, "last_error": str(exc),
                               "last_run": datetime.now(timezone.utc)})
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    create_tables()
    # Migrate: add game and point columns if they don't exist yet
    from sqlalchemy import text
    with SessionLocal() as _db:
        try:
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS game VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS point FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS commence_time TIMESTAMPTZ"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS source_type VARCHAR DEFAULT 'sportsbook'"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS adjusted_prob FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS adj_flags VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS game_id VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS implied_prob FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS opening_odds INTEGER"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS player_name VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS is_prop BOOLEAN DEFAULT FALSE"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS analysis TEXT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS analysis_generated_at TIMESTAMPTZ"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS confidence_score FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS kelly_pct FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS bet_pct FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS money_pct FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS sharp_score FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS proj_away_score FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS proj_home_score FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS proj_total FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS proj_home_win_prob FLOAT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS proj_away_display VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS proj_home_display VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS home_trend VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS away_trend VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS all_book_odds TEXT"))
            _db.execute(text("ALTER TABLE daily_picks ADD COLUMN IF NOT EXISTS player_name VARCHAR"))
            _db.execute(text("ALTER TABLE daily_picks ADD COLUMN IF NOT EXISTS is_prop BOOLEAN DEFAULT FALSE"))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate: fix home_trend/away_trend column types — they may have been
    # created as DOUBLE PRECISION instead of TEXT in some deployments.
    # ALTER COLUMN ... TYPE TEXT USING ::TEXT safely converts any existing values.
    with SessionLocal() as _db:
        try:
            _db.execute(text(
                "ALTER TABLE ev_bet_cache "
                "ALTER COLUMN home_trend TYPE TEXT USING home_trend::TEXT"
            ))
            _db.execute(text(
                "ALTER TABLE ev_bet_cache "
                "ALTER COLUMN away_trend TYPE TEXT USING away_trend::TEXT"
            ))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate: add trial_ends_at to users table
    with SessionLocal() as _db:
        try:
            _db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ"))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate: add game_id to daily_picks for CLV closing line lookup
    with SessionLocal() as _db:
        try:
            _db.execute(text("ALTER TABLE daily_picks ADD COLUMN IF NOT EXISTS game_id VARCHAR"))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate: create odds_history table (append-only CLV ledger)
    with SessionLocal() as _db:
        try:
            _db.execute(text("""
                CREATE TABLE IF NOT EXISTS odds_history (
                    id            SERIAL PRIMARY KEY,
                    game_id       VARCHAR NOT NULL,
                    league        VARCHAR NOT NULL,
                    market        VARCHAR NOT NULL,
                    team          VARCHAR NOT NULL,
                    game          VARCHAR,
                    point         FLOAT,
                    book          VARCHAR NOT NULL,
                    odds          INTEGER NOT NULL,
                    implied_prob  FLOAT,
                    true_prob     FLOAT,
                    ev_percent    FLOAT,
                    commence_time TIMESTAMPTZ,
                    captured_at   TIMESTAMPTZ NOT NULL
                )
            """))
            _db.execute(text("CREATE INDEX IF NOT EXISTS ix_odds_history_game_id ON odds_history (game_id)"))
            _db.execute(text("CREATE INDEX IF NOT EXISTS ix_odds_history_captured_at ON odds_history (captured_at)"))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate: create daily_picks table for existing deployments
    with SessionLocal() as _db:
        try:
            _db.execute(text("""
                CREATE TABLE IF NOT EXISTS daily_picks (
                    id            SERIAL PRIMARY KEY,
                    pick_date     DATE UNIQUE NOT NULL,
                    league        VARCHAR,
                    market        VARCHAR,
                    team          VARCHAR,
                    game          VARCHAR,
                    point         FLOAT,
                    book          VARCHAR,
                    source_type   VARCHAR DEFAULT 'sportsbook',
                    ev_percent    FLOAT,
                    true_prob     FLOAT,
                    odds          INTEGER,
                    commence_time TIMESTAMPTZ,
                    synopsis      TEXT,
                    sent_at       TIMESTAMPTZ,
                    result        VARCHAR
                )
            """))
            _db.commit()
        except Exception:
            _db.rollback()

    # Stripe subscription sync — runs hourly to heal users whose DB record
    # was never updated after a successful Stripe checkout (webhook/success miss).
    scheduler.add_job(
        sync_stripe_subscriptions,
        trigger=IntervalTrigger(hours=1),
        id="stripe_sync",
        name="Sync Stripe subscriptions (hourly)",
        next_run_time=datetime.now(timezone.utc),   # run once at startup too
        misfire_grace_time=300,
        replace_existing=True,
    )

    # Every-30-min refresh — runs at startup then every 30 minutes
    scheduler.add_job(
        refresh_ev_cache,
        trigger=IntervalTrigger(minutes=30),
        id="ev_cache_refresh",
        name="Refresh EV bet cache (every 30 min)",
        next_run_time=datetime.now(timezone.utc),   # run once at startup
        misfire_grace_time=120,
        replace_existing=True,
    )
    # Pre-newsletter refresh — runs at 7:59 AM CT so the cache is fresh
    # for the 8:00 AM newsletter send
    scheduler.add_job(
        refresh_ev_cache,
        trigger=CronTrigger(hour=7, minute=59, timezone="America/Chicago"),
        id="ev_cache_prenewsletter",
        name="Refresh EV bet cache (pre-newsletter 7:59 AM CT)",
        replace_existing=True,
        misfire_grace_time=60,
    )
    # Schedule daily newsletter at 8:00 AM CT
    scheduler.add_job(
        send_daily_newsletter,
        trigger=CronTrigger(hour=8, minute=0, timezone="America/Chicago"),
        id="daily_newsletter",
        name="Daily newsletter at 8 AM CT",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Weekly OddsHistory purge — runs daily at 3 AM CT, removes rows older than 14 days
    scheduler.add_job(
        purge_old_odds_history,
        trigger=CronTrigger(hour=3, minute=0, timezone="America/Chicago"),
        id="odds_history_purge",
        name="Purge OddsHistory rows older than 14 days (daily at 3 AM CT)",
        replace_existing=True,
    )

    scheduler.start()
    log.info("APScheduler started — EV cache refreshes every 30 min + 7:59 AM CT pre-newsletter, newsletter sends at 8 AM CT.")

    # Start Telegram bot in a background daemon thread (runs its own event loop)
    _start_telegram_bot()


def _start_telegram_bot() -> None:
    """Launch telegram_bot in a daemon thread with its own event loop.

    run_polling() installs OS signal handlers which only work on the main
    thread, so we call run_polling_async() instead — it uses the async
    context-manager API and never touches signal handlers.
    """
    import threading, sys as _sys, asyncio

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        log.info("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return

    def _run():
        # Give the thread its own event loop so asyncio doesn't complain.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
            from telegram_bot import run_polling_async
            log.info("Telegram bot starting…")
            loop.run_until_complete(run_polling_async())
        except Exception as exc:
            log.error("Telegram bot crashed: %s", exc, exc_info=True)
        finally:
            loop.close()

    t = threading.Thread(target=_run, name="telegram-bot", daemon=True)
    t.start()
    log.info("Telegram bot thread launched.")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    scheduler.shutdown(wait=False)
    log.info("APScheduler stopped.")


# ---------------------------------------------------------------------------
# Subscription middleware
# ---------------------------------------------------------------------------

class SubscriptionMiddleware(BaseHTTPMiddleware):
    """
    Intercepts every request to /dashboard.

    1. Missing / invalid JWT        → redirect /login
    2. User not subscribed AND
       not within active trial window → redirect /pricing
    3. Subscribed OR in active trial → pass through

    Trial safety net: if is_subscribed is False but trial_ends_at is set
    and still in the future, the user is mid-trial and gets full access.
    The flag is healed in-place so subsequent requests skip this check.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        protected = ("/dashboard", "/welcome")
        if not any(request.url.path.startswith(p) for p in protected):
            return await call_next(request)

        token = get_token_from_request(request)
        if not token:
            return RedirectResponse(url="/login", status_code=303)

        payload = decode_access_token(token)
        if not payload:
            return RedirectResponse(url="/login", status_code=303)

        db: Session = SessionLocal()
        try:
            user = db.query(User).filter(User.id == int(payload["sub"])).first()
            if not user:
                return RedirectResponse(url="/login", status_code=303)

            if not user.is_subscribed:
                # Safety net: honor an active trial window even if the
                # is_subscribed flag is wrong (webhook ordering / race condition)
                trial_ends = getattr(user, "trial_ends_at", None)
                now_utc    = datetime.now(timezone.utc)
                if trial_ends and trial_ends > now_utc:
                    # Mid-trial — heal the flag so future requests skip this branch
                    try:
                        user.is_subscribed = True
                        db.commit()
                        log.info(
                            "SubscriptionMiddleware: healed is_subscribed for %s "
                            "(trial active until %s)",
                            user.email, trial_ends.isoformat(),
                        )
                    except Exception as _heal_exc:
                        db.rollback()
                        log.warning("SubscriptionMiddleware: flag heal failed: %s", _heal_exc)
                    # Allow through regardless of whether the heal succeeded
                else:
                    return RedirectResponse(url="/pricing", status_code=303)
        finally:
            db.close()

        return await call_next(request)


app.add_middleware(SubscriptionMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "dev-secret-change-in-production"),
    session_cookie="positev_admin_session",
    max_age=86400 * 7,   # 7-day session
    https_only=False,    # Railway terminates TLS at the proxy layer
    same_site="lax",
)


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

def purge_old_odds_history() -> None:
    """Delete OddsHistory rows older than 14 days — games have long since closed."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    with SessionLocal() as db:
        try:
            deleted = db.query(OddsHistory).filter(OddsHistory.captured_at < cutoff).delete()
            db.commit()
            log.info("OddsHistory purge: removed %d rows older than 14 days.", deleted)
        except Exception as exc:
            db.rollback()
            log.error("OddsHistory purge failed: %s", exc)


def compute_clv(db: Session, pick) -> Optional[float]:
    """
    Compute actual CLV for a DailyPick using the stored closing line proxy.

    CLV (%) = (closing_implied_prob - pick_implied_prob) * 100
    Positive = you beat the closing line (good).
    Negative = closing line was better than your price (bad).

    Returns None if no OddsHistory data is available (e.g. first day of deploy).
    """
    if not getattr(pick, "game_id", None) or not pick.commence_time:
        return None

    def _american_to_implied(odds: int) -> float:
        if odds > 0:
            return 100 / (odds + 100)
        return abs(odds) / (abs(odds) + 100)

    closing = (
        db.query(OddsHistory.odds, OddsHistory.implied_prob)
        .filter(
            OddsHistory.game_id == pick.game_id,
            OddsHistory.book    == pick.book,
            OddsHistory.market  == pick.market,
            OddsHistory.team    == pick.team,
            OddsHistory.captured_at < pick.commence_time,
        )
        .order_by(OddsHistory.captured_at.desc())
        .first()
    )
    if not closing:
        return None

    pick_implied    = _american_to_implied(pick.odds) if pick.odds else None
    closing_implied = closing[1] or (_american_to_implied(closing[0]) if closing[0] else None)
    if pick_implied is None or closing_implied is None:
        return None
    return round((closing_implied - pick_implied) * 100, 2)


def _compute_pick_record(picks) -> dict:
    """
    Compute hypothetical P&L for a flat-bet model.
    Bankroll: $1,000  |  Unit: $20  |  Flat bet (never changes)
    Won (+odds): profit = unit * (odds / 100)
    Won (-odds): profit = unit * (100 / abs(odds))
    Lost:        profit = -unit
    Push:        profit = 0
    """
    UNIT, BANKROLL = 20.0, 1000.0
    wins = losses = pushes = 0
    total_profit = 0.0
    for pick in picks:
        result = (pick.result or "").lower()
        odds = pick.odds
        if result == "won":
            wins += 1
            if odds is not None:
                total_profit += UNIT * (odds / 100) if odds > 0 else UNIT * (100 / abs(odds))
        elif result == "lost":
            losses += 1
            total_profit -= UNIT
        elif result == "push":
            pushes += 1
    total_picks = wins + losses + pushes
    roi = (total_profit / BANKROLL) * 100 if total_picks > 0 else 0.0
    units = total_profit / UNIT
    return {
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "total_picks":  total_picks,
        "total_profit": round(total_profit, 2),
        "roi":          round(roi, 2),
        "units":        round(units, 2),
        "has_data":     total_picks > 0,
    }


# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Railway / uptime-monitor health check — returns 200 while app is alive."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}



# ---------------------------------------------------------------------------
# AI Analysis endpoint
# ---------------------------------------------------------------------------

def _projection_supports_bet(bet_row, proj: dict) -> bool:
    """
    Return True only when the model projection supports the bet direction.
    Suppresses display when the model clearly contradicts the pick.

    The MLB Pythagorean model is a rough estimate and always shown regardless
    of direction (it's not precise enough to confidently suppress bets).

    Rules (Optimal model only):
      h2h      — model win-probability favours the same team as the bet
      totals   — model total is on the same side (over/under) as the bet line
      spreads  — model margin is larger/smaller than the spread in the right direction
    """
    # Local Pythagorean model — always surface it; too coarse to suppress bets
    if proj.get("source") == "mlb_pythagorean":
        return True
    market      = bet_row.market or ""
    team        = (bet_row.team or "").strip()
    point       = bet_row.point
    game        = bet_row.game or ""

    spread_mean    = proj.get("spread_mean")      # positive = home team margin
    total_mean     = proj.get("total_mean")
    home_win_prob  = proj.get("home_win_probability")

    # Determine home team keyword for is_home_bet check
    try:
        _, home_str = game.split(" @ ", 1)
        home_kw = home_str.strip().split()[-1].lower()
        is_home_bet = home_kw in team.lower()
    except Exception:
        return True  # can't parse → show anyway

    if market == "h2h" and home_win_prob is not None:
        return home_win_prob > 0.5 if is_home_bet else home_win_prob < 0.5

    if market == "totals" and total_mean is not None and point is not None:
        is_over = team.lower().startswith("over")
        return total_mean > point if is_over else total_mean < point

    if market == "spreads" and spread_mean is not None and point is not None:
        # spread_mean = home team winning margin (positive = home wins by that much)
        # Home bet -3.5: need spread_mean > 3.5 (home wins by more than 3.5)
        # Away bet -3.5: need away team to win by >3.5, i.e. spread_mean < -3.5
        threshold = abs(point)
        return spread_mean > threshold if is_home_bet else spread_mean < -threshold

    return True  # unknown market → show anyway


@app.get("/api/projection/{bet_id}")
async def get_projection(bet_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Return Optimal game-level score projections for a specific bet.

    Looks up the EVBetCache row, resolves the game string and league, then
    queries Optimal for spread / total / score projections and the model's
    home-win probability. Subscription required.

    Response JSON:
        {
          "away_team": "Houston Rockets",
          "home_team": "Golden State Warriors",
          "away_score_mean": 115.3,
          "home_score_mean": 111.6,
          "spread_mean": -3.7,        # positive = home favoured
          "total_mean": 226.9,
          "home_win_probability": 0.41,
          "consensus_line": 3.5,
          "consensus_total": 226.0,
          "consensus_home_ml": "140",
          "consensus_away_ml": "-165",
          "updated_at": "2026-04-05T…"
        }
    """
    # Auth check — subscription required
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_subscribed:
        raise HTTPException(status_code=403, detail="Subscription required")

    bet_row = db.query(EVBetCache).filter(EVBetCache.id == bet_id).first()
    if not bet_row:
        raise HTTPException(status_code=404, detail="Bet not found")

    # Props and bets without a game string don't have game projections
    if bet_row.is_prop or not bet_row.game or " @ " not in (bet_row.game or ""):
        raise HTTPException(status_code=422, detail="No game projection for this bet type")

    import asyncio
    from scripts.context_fetcher import fetch_game_projections, fetch_game_context as _fgc

    cache_key = f"{bet_row.league}:{bet_row.game}"
    cached = _proj_cache_get(cache_key)
    if cached:
        log.info("Projection cache HIT for %s", cache_key)
        return JSONResponse(cached)

    log.info("Projection request: game=%r league=%r bet_id=%d", bet_row.game, bet_row.league, bet_id)

    loop = asyncio.get_event_loop()

    # ── Fast path: use projection data already stored on this row ────────────
    # The pipeline writes proj_home_score / proj_away_score / proj_total /
    # proj_home_win_prob at run time. Serving those directly avoids a live
    # round-trip to mcp.tangiers.ai (which can take 10-25 s on a slow day).
    # We still fetch ESPN context (records, trends, injuries) live — it's fast
    # and provides the freshest situational data.
    _has_pipeline_proj = (
        bet_row.proj_home_score is not None
        or bet_row.proj_away_score is not None
        or bet_row.proj_total    is not None
    )

    if _has_pipeline_proj:
        log.info("Projection fast-path (pipeline data) for bet_id=%d", bet_id)
        # Build proj dict from stored fields — no live Optimal call needed
        game_parts  = (bet_row.game or "").split(" @ ", 1)
        away_str    = game_parts[0].strip() if len(game_parts) > 1 else ""
        home_str    = game_parts[1].strip() if len(game_parts) > 1 else ""
        proj: dict = {
            "away_team":            away_str,
            "home_team":            home_str,
            "away_display":         bet_row.proj_away_display or away_str,
            "home_display":         bet_row.proj_home_display or home_str,
            "home_score_mean":      bet_row.proj_home_score,
            "away_score_mean":      bet_row.proj_away_score,
            "total_mean":           bet_row.proj_total,
            "home_win_probability": bet_row.proj_home_win_prob,
        }
        # Derive spread from scores when available
        if bet_row.proj_home_score is not None and bet_row.proj_away_score is not None:
            proj["spread_mean"] = round(bet_row.proj_home_score - bet_row.proj_away_score, 2)

        # Fetch ESPN context live (fast, ~2 s) for records/trends/injuries
        try:
            ctx = await asyncio.wait_for(
                loop.run_in_executor(None, _fgc, bet_row.game, bet_row.league, bet_row.commence_time),
                timeout=8.0,
            )
        except Exception as _ctx_err:
            log.warning("Context fetch skipped for bet_id=%d: %s", bet_id, _ctx_err)
            ctx = {}

    else:
        # ── Slow path: no pipeline data — hit Optimal + ESPN live ────────────
        log.info("Projection live-fetch (no pipeline data) for bet_id=%d", bet_id)
        proj_future = loop.run_in_executor(
            None, fetch_game_projections, bet_row.game, bet_row.league
        )
        ctx_future = loop.run_in_executor(
            None, _fgc, bet_row.game, bet_row.league, bet_row.commence_time
        )
        results = await asyncio.gather(proj_future, ctx_future, return_exceptions=True)
        proj = results[0] if not isinstance(results[0], Exception) else None
        ctx  = results[1] if not isinstance(results[1], Exception) else {}

        if isinstance(results[0], Exception):
            log.error("Projection fetch error for bet_id=%d: %s", bet_id, results[0])
        if isinstance(results[1], Exception):
            log.warning("Context fetch error for bet_id=%d: %s", bet_id, results[1])

        if not proj and not ctx:
            raise HTTPException(
                status_code=503,
                detail="Projection service is temporarily unavailable — please try again in a moment"
            )

    # Merge: context base + projection overrides (projection wins on conflicts)
    merged = dict(ctx or {})
    merged.update({k: v for k, v in (proj or {}).items() if v is not None})

    # Derive individual score means from spread+total when Optimal doesn't
    # provide homeScore/awayScore separately (common for NHL and soccer).
    # home_score = (total + spread) / 2,  away_score = (total - spread) / 2
    if (
        merged.get("home_score_mean") is None
        and merged.get("total_mean") is not None
        and merged.get("spread_mean") is not None
    ):
        _t = float(merged["total_mean"])
        _s = float(merged["spread_mean"])
        merged["home_score_mean"] = round((_t + _s) / 2, 2)
        merged["away_score_mean"] = round((_t - _s) / 2, 2)

    # Determine if the bet is on the home or away side
    market    = bet_row.market or ""
    team      = (bet_row.team or "").strip()
    point     = bet_row.point
    game_str  = bet_row.game or ""
    g_parts   = game_str.split(" @ ", 1)
    away_team = g_parts[0].strip() if len(g_parts) > 1 else ""
    home_team = g_parts[1].strip() if len(g_parts) > 1 else ""

    def _word_set(s: str) -> set:
        skip = {"at", "the", "a", "an", "vs", "fc", "sc", "city", "state"}
        return {w for w in s.lower().split() if w not in skip and len(w) > 2}

    is_home_bet = bool(home_team) and bool(_word_set(home_team) & _word_set(team))

    model_agrees: Optional[bool] = None

    if market == "totals":
        total_mean = merged.get("total_mean")
        if total_mean is not None and point is not None:
            is_over      = team.lower().startswith("over")
            model_agrees = total_mean > point if is_over else total_mean < point

    elif market == "spreads":
        # Derive margin from scores for consistency with what the card displays
        hs_ep = merged.get("home_score_mean")
        as_ep = merged.get("away_score_mean")
        spread_mean = (hs_ep - as_ep) if (hs_ep is not None and as_ep is not None) else merged.get("spread_mean")
        if spread_mean is not None and point is not None:
            model_agrees = spread_mean > -point if is_home_bet else spread_mean < point

    elif market == "h2h":
        # Prefer projected scores — they're what the card displays and are unambiguous.
        # Fall back to home_win_probability only when scores aren't available.
        hs_h2h = merged.get("home_score_mean")
        as_h2h = merged.get("away_score_mean")
        if hs_h2h is not None and as_h2h is not None:
            # Model agrees with the bet if the betted team is projected to score more
            model_agrees = hs_h2h > as_h2h if is_home_bet else as_h2h > hs_h2h
        else:
            home_win_prob = merged.get("home_win_probability")
            if home_win_prob is not None:
                model_agrees = home_win_prob > 0.5 if is_home_bet else home_win_prob < 0.5

    if model_agrees is not None:
        merged["model_agrees_with_bet"] = model_agrees
        if not model_agrees:
            log.info(
                "Projection contradicts bet for bet_id=%d market=%s team=%r "
                "(is_home=%s spread=%.2f total=%.1f home_wp=%.2f point=%s)",
                bet_id, market, team, is_home_bet,
                merged.get("spread_mean") or 0,
                merged.get("total_mean") or 0,
                merged.get("home_win_probability") or 0,
                point,
            )

    # Cache successful result so repeat requests are instant
    if merged:
        _proj_cache_set(cache_key, merged)

    return JSONResponse(merged)


@app.get("/api/prop-context/{bet_id}")
async def get_prop_context(bet_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Return sport-specific prop context for a player prop bet card.

    Routes by sport_key + market:
        baseball_mlb  + pitcher_*  → pitcher platoon splits vs LHB / vs RHB
        baseball_mlb  + batter_*   → batter platoon splits  vs LHP / vs RHP
        icehockey_nhl + player_shots_on_goal → player SOG averages + recent log
        icehockey_nhl + (other)    → opposing goalie stats + recent form

    Subscription required.
    """
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_subscribed:
        raise HTTPException(status_code=403, detail="Subscription required")

    bet_row = db.query(EVBetCache).filter(EVBetCache.id == bet_id).first()
    if not bet_row:
        raise HTTPException(status_code=404, detail="Bet not found")

    if not bet_row.is_prop:
        raise HTTPException(status_code=422, detail="Not a prop bet")

    player_name  = bet_row.player_name or bet_row.team or ""
    prop_market  = bet_row.market or ""
    sport_key    = bet_row.league or ""

    # For pitcher props, try to resolve the pitcher ID from probable pitchers
    # (avoids an extra MLB API search call when we already have the data)
    pitcher_id: int | None = None
    if sport_key == "baseball_mlb" and prop_market.startswith("pitcher_"):
        try:
            from scripts.context_fetcher import fetch_mlb_probable_pitchers, _fetch_mlb_player_id
            pitchers = fetch_mlb_probable_pitchers()
            for _gk, entry in pitchers.items():
                for side in ("home", "away"):
                    pp = entry.get(side) or {}
                    if pp.get("name") and player_name and (
                        player_name.lower() in pp["name"].lower()
                        or pp["name"].lower() in player_name.lower()
                    ):
                        pitcher_id = pp.get("id")
                        break
                if pitcher_id:
                    break
        except Exception as _pe:
            log.warning("prop-context: pitcher ID pre-fetch failed: %s", _pe)

    import asyncio
    from scripts.context_fetcher import fetch_prop_context

    loop = asyncio.get_event_loop()
    try:
        ctx = await asyncio.wait_for(
            loop.run_in_executor(
                None, fetch_prop_context,
                player_name, prop_market, sport_key, pitcher_id,
            ),
            timeout=12.0,
        )
    except asyncio.TimeoutError:
        log.error("prop-context timed out for bet_id=%d", bet_id)
        raise HTTPException(status_code=504, detail="Stat service timed out — try again")
    except Exception as exc:
        log.error("prop-context error for bet_id=%d: %s", bet_id, exc)
        raise HTTPException(status_code=500, detail="Stat fetch failed")

    if not ctx or ctx.get("prop_context_type") in ("none", "no_data"):
        raise HTTPException(
            status_code=422,
            detail=f"No prop context available for {prop_market} ({sport_key})",
        )

    return JSONResponse(ctx)


@app.get("/api/analysis/{bet_id}")
@app.get("/api/analyze/{bet_id}")   # alias — keep both working
async def get_analysis(bet_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Return AI analysis for a specific bet (by EVBetCache.id).

    - If analysis already stored in DB (and generated within 6 hours): return cached.
    - Otherwise: call ai_analyzer.analyze_bet(), store result, return.
    - Requires valid subscription (checked via JWT cookie).

    Response JSON:
        {
          "analysis": "...",
          "confidence_score": 78,
          "kelly_pct": 2.1,
          "true_prob_refined": 0.412,
          "ev_pct_refined": 6.3,
          "cached": false
        }
    """
    from datetime import timedelta

    bet_row = db.query(EVBetCache).filter(EVBetCache.id == bet_id).first()
    if not bet_row:
        raise HTTPException(status_code=404, detail="Bet not found")

    # Return cached analysis if fresh (< 6 hours old)
    cache_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    if (
        bet_row.analysis
        and bet_row.analysis_generated_at
        and bet_row.analysis_generated_at > cache_cutoff
    ):
        return JSONResponse({
            "analysis":          bet_row.analysis,
            "confidence_score":  bet_row.confidence_score,
            "kelly_pct":         bet_row.kelly_pct,
            "cached":            True,
            "edge_tag":          "",
        })

    # Build bet dict for analyzer — include ALL rich pipeline fields so Claude
    # has full context even when the Optimal/ESPN live fetches return sparse data.
    bet_dict = {
        # Core identifiers
        "id":               bet_row.id,
        "game":             bet_row.game or "",
        "league":           bet_row.league or "",
        "market":           bet_row.market or "",
        "team":             bet_row.team or "",
        "odds":             bet_row.odds or 0,
        "true_prob":        bet_row.true_prob or 0.5,
        "ev_percent":       bet_row.ev_percent or 0.0,
        "point":            bet_row.point,
        "player_name":      bet_row.player_name,
        "is_prop":          bool(bet_row.is_prop),
        "commence_time":    bet_row.commence_time,
        # Probability breakdown
        "implied_prob":     bet_row.implied_prob,
        "adjusted_prob":    bet_row.adjusted_prob,
        "adj_flags":        bet_row.adj_flags or "",
        # Line movement (CLV signal)
        "opening_odds":     bet_row.opening_odds,
        # Sharp money / public betting splits (Action Network)
        "bet_pct":          bet_row.bet_pct,
        "money_pct":        bet_row.money_pct,
        "sharp_score":      bet_row.sharp_score,
        # Model projections computed at pipeline time
        "proj_home_score":  bet_row.proj_home_score,
        "proj_away_score":  bet_row.proj_away_score,
        "proj_total":       bet_row.proj_total,
        "proj_home_win_prob": bet_row.proj_home_win_prob,
        # Recent form strings (e.g. "7-3 W4")
        "home_trend":       bet_row.home_trend or "",
        "away_trend":       bet_row.away_trend or "",
        # Full market odds across all books
        "all_book_odds":    bet_row.all_book_odds or "",
        # Source type (sportsbook vs exchange/prediction market)
        "source_type":      bet_row.source_type or "sportsbook",
    }

    # Run analysis in a thread (it's sync/blocking)
    import asyncio
    from models.ai_analyzer import analyze_bet

    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, analyze_bet, bet_dict),
            timeout=55.0,   # stay under Railway's 60s hard timeout
        )
    except asyncio.TimeoutError:
        log.error("AI analysis timed out for bet_id=%d", bet_id)
        raise HTTPException(status_code=504, detail="Analysis timed out — try again")
    except Exception as exc:
        log.error("AI analysis failed for bet_id=%d: %s", bet_id, exc)
        raise HTTPException(status_code=500, detail="Analysis generation failed")

    if result is None:
        raise HTTPException(status_code=502, detail="Analysis service unavailable")

    # Persist to DB
    try:
        bet_row.analysis               = result["analysis"]
        bet_row.analysis_generated_at  = datetime.now(timezone.utc)
        bet_row.confidence_score       = result["confidence_score"]
        bet_row.kelly_pct              = result["kelly_pct"]
        db.commit()
    except Exception as exc:
        log.warning("Failed to cache analysis for bet_id=%d: %s", bet_id, exc)
        db.rollback()

    return JSONResponse({
        "analysis":          result["analysis"],
        "confidence_score":  result["confidence_score"],
        "kelly_pct":         result["kelly_pct"],
        "edge_tag":          result.get("edge_tag", ""),
        "cached":            False,
    })


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, db: Session = Depends(get_db)):
    all_picks = (
        db.query(DailyPick)
        .order_by(DailyPick.pick_date.desc())
        .all()
    )
    settled  = [p for p in all_picks if p.result in ("won", "lost", "push")]
    won      = sum(1 for p in settled if p.result == "won")
    lost     = sum(1 for p in settled if p.result == "lost")

    # P&L and ROI based on $20 flat unit size
    # ROI = net profit / total amount staked × 100
    UNIT = 20.0
    total_pl = 0.0
    for p in settled:
        if not p.odds:
            continue
        if p.result == "won":
            total_pl += UNIT * p.odds / 100 if p.odds > 0 else UNIT * 100 / abs(p.odds)
        elif p.result == "lost":
            total_pl -= UNIT
        # push = 0 net, but stake was still risked
    total_pl    = round(total_pl, 2)
    total_units = round(total_pl / UNIT, 2)

    # ROI denominator = staked amount on all decisive bets (won + lost)
    decisive = won + lost
    track_roi = round(total_pl / (UNIT * decisive) * 100, 1) if decisive > 0 else None

    return templates.TemplateResponse(request, "index.html", {
        "track_picks":    all_picks,
        "track_won":      won,
        "track_lost":     lost,
        "track_total":    len(settled),
        "track_roi":      track_roi,
        "track_pl":       total_pl,
        "track_units":    total_units,
    })


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    token = get_token_from_request(request)
    if token and decode_access_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request, db: Session = Depends(get_db)):
    # Optionally identify the logged-in user so the template can show an upgrade banner
    user = None
    token = get_token_from_request(request)
    if token:
        payload = decode_access_token(token)
        if payload:
            user = db.query(User).filter(User.id == int(payload["sub"])).first()
    return templates.TemplateResponse(request, "pricing.html", {"user": user})


# ── Account Settings ──────────────────────────────────────────────────────

def _get_authed_user(request: Request, db: Session):
    """Auth helper for account routes — returns User or None."""
    token = get_token_from_request(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    return db.query(User).filter(User.id == int(payload["sub"])).first()


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, db: Session = Depends(get_db)):
    """Account settings: subscription status, change password, billing portal."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    from zoneinfo import ZoneInfo
    _CT = ZoneInfo("America/Chicago")
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    # Stripe subscription details
    sub_status            = None
    cancel_at_period_end  = False
    period_end_str        = None
    is_trial              = False
    trial_ends_str        = None

    # Trial detection from DB
    trial_ends_at = getattr(user, "trial_ends_at", None)
    now_utc = datetime.now(timezone.utc)
    if trial_ends_at and trial_ends_at > now_utc:
        is_trial = True
        try:
            trial_ends_str = trial_ends_at.astimezone(_CT).strftime("%B %-d, %Y")
        except Exception:
            trial_ends_str = str(trial_ends_at.date())

    # Fetch live Stripe data for period_end and cancel status
    if user.stripe_subscription_id and _stripe.api_key:
        try:
            sub = _stripe.Subscription.retrieve(user.stripe_subscription_id)
            sub_status           = sub.status
            cancel_at_period_end = bool(sub.cancel_at_period_end)
            if sub.current_period_end:
                period_end_dt  = datetime.fromtimestamp(int(sub.current_period_end), tz=_CT)
                period_end_str = period_end_dt.strftime("%B %-d, %Y")
        except Exception as exc:
            log.warning("account_page: Stripe fetch failed for user %s: %s", user.email, exc)

    member_since_str = "—"
    if user.created_at:
        try:
            member_since_str = user.created_at.astimezone(_CT).strftime("%B %-d, %Y")
        except Exception:
            member_since_str = str(user.created_at.date())

    qs = request.query_params
    return templates.TemplateResponse(request, "account.html", {
        "user":                 user,
        "sub_status":           sub_status,
        "cancel_at_period_end": cancel_at_period_end,
        "period_end_str":       period_end_str,
        "is_trial":             is_trial,
        "trial_ends_str":       trial_ends_str,
        "member_since_str":     member_since_str,
        "pw_success":           qs.get("pw_success"),
        "pw_error":             qs.get("pw_error"),
        "cancel_done":          qs.get("cancel_done"),
        "cancel_error":         qs.get("cancel_error"),
        "reactivated":          qs.get("reactivated"),
        "portal_error":         qs.get("portal_error"),
    })


@app.post("/account/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str     = Form(...),
    confirm_password: str = Form(...),
    db: Session           = Depends(get_db),
):
    """Change password — validates current, hashes new, saves."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    from web.auth import verify_password, hash_password

    if not verify_password(current_password, user.hashed_password):
        return RedirectResponse(url="/account?pw_error=wrong_password", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/account?pw_error=mismatch", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(url="/account?pw_error=too_short", status_code=303)

    user.hashed_password = hash_password(new_password)
    db.commit()
    log.info("Password changed for user %s", user.email)
    return RedirectResponse(url="/account?pw_success=1", status_code=303)


@app.post("/account/cancel-subscription")
async def cancel_subscription(request: Request, db: Session = Depends(get_db)):
    """Set cancel_at_period_end=True on Stripe — access retained until period end."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    # If stripe_subscription_id is missing, look it up from Stripe using the
    # customer ID — covers webhook-miss cases where the field was never written.
    if not user.stripe_subscription_id and user.stripe_customer_id:
        try:
            subs = _stripe.Subscription.list(
                customer=user.stripe_customer_id,
                status="all",
                limit=5,
            )
            active = next(
                (s for s in subs.auto_paging_iter()
                 if s.status in ("active", "trialing", "past_due")),
                None,
            )
            if active:
                user.stripe_subscription_id = active.id
                db.commit()
                log.info(
                    "cancel_subscription: recovered stripe_subscription_id=%s for user %s",
                    active.id, user.email,
                )
        except _stripe.error.StripeError as exc:
            log.error("cancel_subscription: Stripe lookup failed for user %s: %s", user.email, exc)

    if not user.stripe_subscription_id:
        return RedirectResponse(url="/account?cancel_error=no_sub", status_code=303)

    try:
        _stripe.Subscription.modify(
            user.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        log.info("Subscription cancel_at_period_end=True for user %s", user.email)
    except _stripe.error.StripeError as exc:
        log.error("Cancel subscription failed for user %s: %s", user.email, exc)
        return RedirectResponse(url="/account?cancel_error=stripe", status_code=303)

    return RedirectResponse(url="/account?cancel_done=1", status_code=303)


@app.post("/account/reactivate-subscription")
async def reactivate_subscription(request: Request, db: Session = Depends(get_db)):
    """Undo a pending cancellation — set cancel_at_period_end=False."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.stripe_subscription_id:
        return RedirectResponse(url="/account", status_code=303)

    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    try:
        _stripe.Subscription.modify(
            user.stripe_subscription_id,
            cancel_at_period_end=False,
        )
        log.info("Subscription reactivated for user %s", user.email)
    except _stripe.error.StripeError as exc:
        log.error("Reactivate subscription failed for user %s: %s", user.email, exc)
        return RedirectResponse(url="/account?cancel_error=stripe", status_code=303)

    return RedirectResponse(url="/account?reactivated=1", status_code=303)


@app.get("/account/billing-portal")
async def billing_portal(request: Request, db: Session = Depends(get_db)):
    """Redirect to Stripe Customer Portal for payment method / invoice management."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.stripe_customer_id:
        return RedirectResponse(url="/account", status_code=303)

    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    try:
        portal = _stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{base_url}/account",
        )
        return RedirectResponse(url=portal.url, status_code=303)
    except _stripe.error.StripeError as exc:
        log.error("Billing portal failed for user %s: %s", user.email, exc)
        return RedirectResponse(url="/account?portal_error=1", status_code=303)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    token = get_token_from_request(request)
    if token and decode_access_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(
    request: Request,
    current_user: User = Depends(require_auth),
):
    """Onboarding walkthrough shown after first login."""
    return templates.TemplateResponse(
        request,
        "welcome.html",
        {"user": current_user},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    welcome: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_auth),
):
    """
    Served only after SubscriptionMiddleware confirms valid JWT + active subscription.
    Reads today's +EV bets from EVBetCache — no live API calls on page load.
    """
    # Only show bets for games that haven't started yet.
    # Rows with NULL commence_time (rare edge case) are included so they're
    # never silently dropped.
    _now_utc = datetime.now(timezone.utc)
    bets = (
        db.query(EVBetCache)
        .filter(
            (EVBetCache.commence_time == None) |  # noqa: E711
            (EVBetCache.commence_time > _now_utc)
        )
        .order_by(EVBetCache.ev_percent.desc())
        .all()
    )

    # If the cache is empty and no refresh is in flight, kick one off now.
    # This handles the case where: (a) a deploy wiped stale rows before new data
    # was written, or (b) the scheduler hasn't fired yet after startup.
    # The _cache_status["running"] guard prevents concurrent duplicates.
    if not bets and not _cache_status.get("running", False):
        _last = _cache_status.get("last_run")
        _stale = _last is None or (datetime.now(timezone.utc) - _last).total_seconds() > 300
        if _stale:
            _job = scheduler.get_job("ev_cache_refresh")
            if _job:
                scheduler.modify_job("ev_cache_refresh", next_run_time=datetime.now(timezone.utc))
                log.info("Dashboard: auto-triggered cache refresh (empty cache detected).")

    # Today's morning pick (CT calendar date)
    from zoneinfo import ZoneInfo
    _CT = ZoneInfo("America/Chicago")
    today_ct = datetime.now(_CT).date()
    today_pick = (
        db.query(DailyPick)
        .filter(DailyPick.pick_date == today_ct)
        .first()
    )

    # Detect if the exact line (game + market + team + point) is still live
    pick_still_live = False
    if today_pick:
        pick_still_live = any(
            b.game   == today_pick.game
            and b.market == today_pick.market
            and b.team   == today_pick.team
            and b.point  == today_pick.point
            for b in bets
        )

    # Compute next scheduled refresh time from the scheduler
    job = scheduler.get_job("ev_cache_refresh")
    next_refresh: Optional[datetime] = job.next_run_time if job else None

    # Compute trial days remaining (None if not on trial or trial expired)
    trial_days_remaining: Optional[int] = None
    trial_ends_at: Optional[datetime]   = None
    if getattr(current_user, "trial_ends_at", None):
        now_utc = datetime.now(timezone.utc)
        delta   = current_user.trial_ends_at - now_utc
        if delta.total_seconds() > 0:
            trial_ends_at        = current_user.trial_ends_at
            trial_days_remaining = max(1, delta.days + (1 if delta.seconds > 0 else 0))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user":                 current_user,
            "bets":                 bets,
            "bet_count":            len(bets),
            "cache_status":         _cache_status,
            "next_refresh":         next_refresh,
            "show_welcome":         welcome == "1",
            "today_pick":           today_pick,
            "pick_still_live":      pick_still_live,
            "trial_days_remaining": trial_days_remaining,
            "trial_ends_at":        trial_ends_at,
        },
    )


# ---------------------------------------------------------------------------
# Admin PIN login / logout
# ---------------------------------------------------------------------------

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, error: str = ""):
    """Serve the 6-digit PIN entry page."""
    if _is_admin(request):
        return RedirectResponse(url="/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {"error": error})


@app.post("/admin/login")
async def admin_login_submit(request: Request, pin: str = Form(...)):
    """Verify the 6-digit PIN and start an admin session."""
    admin_pin = os.getenv("ADMIN_PIN", "")
    if not admin_pin:
        log.error("ADMIN_PIN env var not set — admin login disabled")
        return templates.TemplateResponse(
            request, "admin_login.html",
            {"error": "Admin login is not configured. Set ADMIN_PIN in Railway."},
        )
    if secrets.compare_digest(pin.strip(), admin_pin.strip()):
        request.session["admin_authenticated"] = True
        log.info("Admin session started from %s", request.client.host if request.client else "unknown")
        return RedirectResponse(url="/admin", status_code=303)
    log.warning("Failed admin PIN attempt from %s", request.client.host if request.client else "unknown")
    return templates.TemplateResponse(
        request, "admin_login.html",
        {"error": "Incorrect PIN. Try again."},
        status_code=401,
    )


@app.post("/admin/logout")
async def admin_logout(request: Request):
    """Clear the admin session."""
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


# ---------------------------------------------------------------------------
# Manual cache refresh (admin)
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    tier: str = "all",   # "all" | "paid" | "free"
    q: str = "",         # email substring search
    page: int = 1,
):
    """Admin dashboard — protected by session-based PIN auth."""
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    PAGE_SIZE = 25
    page = max(1, page)

    newsletter_subs = (
        db.query(NewsletterSubscriber)
        .order_by(NewsletterSubscriber.subscribed_at.desc())
        .all()
    )

    # Global stats — always unfiltered counts (efficient, no full table load)
    _now_admin = datetime.now(timezone.utc)
    user_total = db.query(User).count()
    # "In trial" = active trial_ends_at, regardless of is_subscribed (the
    # SubscriptionMiddleware heals is_subscribed on next login, so a freshly-
    # activated trial may briefly have is_subscribed=False in the DB).
    _active_trial_cond = (
        User.trial_ends_at.isnot(None) & (User.trial_ends_at > _now_admin)
    )
    user_trial = db.query(User).filter(_active_trial_cond).count()
    # Paid via Stripe: subscribed + stripe sub + no active trial
    user_paid_stripe = db.query(User).filter(
        User.is_subscribed.is_(True),
        User.stripe_subscription_id.isnot(None),
        ~_active_trial_cond,
    ).count()
    # Comped: subscribed + no stripe sub + no active trial
    user_comped = db.query(User).filter(
        User.is_subscribed.is_(True),
        User.stripe_subscription_id.is_(None),
        ~_active_trial_cond,
    ).count()
    user_paid  = user_paid_stripe + user_comped   # backward-compat total (excl. trial)
    user_free  = user_total - user_paid - user_trial

    # Build filtered query — tier filter matches the counter logic exactly
    query = db.query(User)
    if tier == "trial":
        query = query.filter(_active_trial_cond)
    elif tier == "paid":
        query = query.filter(
            User.is_subscribed.is_(True),
            User.stripe_subscription_id.isnot(None),
            ~_active_trial_cond,
        )
    elif tier == "comped":
        query = query.filter(
            User.is_subscribed.is_(True),
            User.stripe_subscription_id.is_(None),
            ~_active_trial_cond,
        )
    elif tier == "free":
        query = query.filter(
            User.is_subscribed.is_(False),
            ~_active_trial_cond,
        )
    if q and q.strip():
        query = query.filter(User.email.ilike(f"%{q.strip()}%"))
    query = query.order_by(User.id.desc())

    filtered_total = query.count()
    total_pages    = max(1, math.ceil(filtered_total / PAGE_SIZE))
    page           = min(page, total_pages)
    users          = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()

    # Daily picks — all rows newest first for the record section
    daily_picks_all = (
        db.query(DailyPick).order_by(DailyPick.pick_date.desc()).all()
    )
    settled = [p for p in daily_picks_all if p.result in ("won", "lost", "push")]
    picks_badge = (
        f"{sum(1 for p in settled if p.result == 'won')}-"
        f"{sum(1 for p in settled if p.result == 'lost')}-"
        f"{sum(1 for p in settled if p.result == 'push')}"
    )

    # CLV data for each past pick (games that have started)
    from datetime import timezone as _tz
    _now_utc = datetime.now(timezone.utc)
    picks_clv = {}
    for pick in daily_picks_all:
        if pick.commence_time and pick.commence_time < _now_utc:
            clv_val = compute_clv(db, pick)
            picks_clv[pick.id] = clv_val
        else:
            picks_clv[pick.id] = None

    # CLV summary stats (only picks with actual CLV data)
    clv_values = [v for v in picks_clv.values() if v is not None]
    clv_beat_count = sum(1 for v in clv_values if v > 0)
    clv_summary = {
        "total":      len(clv_values),
        "beat_count": clv_beat_count,
        "beat_rate":  round(clv_beat_count / len(clv_values) * 100, 1) if clv_values else None,
        "avg_clv":    round(sum(clv_values) / len(clv_values), 2) if clv_values else None,
    }

    # ── Growth metrics ──────────────────────────────────────────────────────
    _seven_ago  = _now_utc - timedelta(days=7)
    _thirty_ago = _now_utc - timedelta(days=30)
    nl_active_count = sum(1 for s in newsletter_subs if s.is_active)
    new_nl_7d   = sum(1 for s in newsletter_subs if s.subscribed_at and s.subscribed_at > _seven_ago)
    new_nl_30d  = sum(1 for s in newsletter_subs if s.subscribed_at and s.subscribed_at > _thirty_ago)
    new_users_7d  = db.query(User).filter(User.created_at > _seven_ago).count()
    new_users_30d = db.query(User).filter(User.created_at > _thirty_ago).count()
    nl_unsub_count   = sum(1 for s in newsletter_subs if not s.is_active)
    conversion_rate  = round(user_paid_stripe / user_total * 100, 1) if user_total > 0 else 0.0
    nl_to_user_rate  = round(user_total / len(newsletter_subs) * 100, 1) if newsletter_subs else 0.0

    # ── Pipeline health ──────────────────────────────────────────────────────
    ev_bets_count  = db.query(EVBetCache).count()
    _ev_leagues    = db.query(EVBetCache.league).distinct().all()
    sports_active  = [r[0] for r in _ev_leagues]
    _last_cache    = db.query(EVBetCache.created_at).order_by(EVBetCache.created_at.desc()).first()
    if _last_cache and _last_cache[0]:
        from zoneinfo import ZoneInfo as _ZI
        _ct_dt     = _last_cache[0].astimezone(_ZI("America/Chicago"))
        last_cache_at = _ct_dt.strftime("%b %-d at %-I:%M %p CT")
    else:
        last_cache_at = "—"
    _ev_rows       = db.query(EVBetCache.ev_percent, EVBetCache.odds).all()
    _ev_vals       = [r[0] for r in _ev_rows if r[0] is not None]
    _odds_vals     = [r[1] for r in _ev_rows if r[1] is not None]
    cache_avg_ev   = round(sum(_ev_vals) / len(_ev_vals), 2) if _ev_vals else None
    cache_avg_odds = round(sum(_odds_vals) / len(_odds_vals)) if _odds_vals else None

    # ── Model performance ────────────────────────────────────────────────────
    _sport_label_map = {
        "basketball_nba": "NBA", "icehockey_nhl": "NHL", "baseball_mlb": "MLB",
        "soccer_epl": "EPL", "soccer_spain_la_liga": "La Liga",
        "soccer_germany_bundesliga": "Bundesliga", "soccer_usa_mls": "MLS",
        "americanfootball_nfl": "NFL",
    }
    total_won_count    = sum(1 for p in settled if p.result == "won")
    total_lost_count   = sum(1 for p in settled if p.result == "lost")
    total_push_count   = sum(1 for p in settled if p.result == "push")
    total_settled_count = len(settled)
    model_win_rate = round(total_won_count / total_settled_count * 100, 1) if total_settled_count > 0 else None
    _ev_picks    = [p.ev_percent for p in daily_picks_all if p.ev_percent is not None]
    picks_avg_ev = round(sum(_ev_picks) / len(_ev_picks), 2) if _ev_picks else None
    _UNIT = 20.0
    picks_by_sport: dict = {}
    for _pick in daily_picks_all:
        _k = _pick.league or "other"
        if _k not in picks_by_sport:
            picks_by_sport[_k] = {"label": _sport_label_map.get(_k, _k.upper()), "won": 0, "lost": 0, "push": 0, "pending": 0, "profit": 0.0}
        if   _pick.result == "won":
            picks_by_sport[_k]["won"]  += 1
            _o = _pick.odds or 0
            picks_by_sport[_k]["profit"] += _UNIT * (_o / 100) if _o > 0 else _UNIT * (100 / abs(_o)) if _o < 0 else 0
        elif _pick.result == "lost":
            picks_by_sport[_k]["lost"]   += 1
            picks_by_sport[_k]["profit"] -= _UNIT
        elif _pick.result == "push":
            picks_by_sport[_k]["push"]   += 1
        else:
            picks_by_sport[_k]["pending"] += 1
    # Round per-sport profit and compute units
    for _sp in picks_by_sport.values():
        _sp["profit"] = round(_sp["profit"], 2)
        _sp["units"]  = round(_sp["profit"] / _UNIT, 2)
    picks_by_sport = dict(sorted(picks_by_sport.items(), key=lambda x: x[1]["won"] + x[1]["lost"], reverse=True))

    # Aggregate P&L / units record
    pick_record = _compute_pick_record(settled)

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "newsletter_subs":  newsletter_subs,
            "users":            users,
            "nl_total":         len(newsletter_subs),
            "nl_active":        nl_active_count,
            # Global counts (unaffected by filter/search)
            "user_total":        user_total,
            "user_paid":         user_paid,
            "user_paid_stripe":  user_paid_stripe,
            "user_trial":        user_trial,
            "user_comped":       user_comped,
            "user_free":         user_free,
            "now_utc_dt":        _now_admin,
            # Pagination metadata
            "filtered_total":   filtered_total,
            "total_pages":      total_pages,
            "current_page":     page,
            "page_size":        PAGE_SIZE,
            # Current filter state (echoed back for URL building in template)
            "current_tier":     tier,
            "current_q":        q,
            # Daily picks record
            "daily_picks_all":  daily_picks_all,
            "picks_badge":      picks_badge,
            "picks_clv":        picks_clv,
            "clv_summary":      clv_summary,
            "now":              datetime.now(timezone.utc).strftime("%b %-d, %Y at %-I:%M %p UTC"),
            "admin_key":        "",   # no longer used
            "is_admin_page":    True,
            # Growth metrics
            "new_nl_7d":        new_nl_7d,
            "new_nl_30d":       new_nl_30d,
            "new_users_7d":     new_users_7d,
            "new_users_30d":    new_users_30d,
            "nl_unsub_count":   nl_unsub_count,
            "conversion_rate":  conversion_rate,
            "nl_to_user_rate":  nl_to_user_rate,
            # Pipeline health
            "ev_bets_count":    ev_bets_count,
            "sports_active":    sports_active,
            "last_cache_at":    last_cache_at,
            "cache_avg_ev":     cache_avg_ev,
            "cache_avg_odds":   cache_avg_odds,
            # PIN embedded in form for hard-refresh fallback button
            "admin_pin_for_form": os.getenv("ADMIN_PIN", ""),
            # Model performance
            "total_won_count":      total_won_count,
            "total_lost_count":     total_lost_count,
            "total_push_count":     total_push_count,
            "total_settled_count":  total_settled_count,
            "model_win_rate":       model_win_rate,
            "picks_avg_ev":         picks_avg_ev,
            "picks_by_sport":       picks_by_sport,
            "pick_record":          pick_record,
        },
    )


@app.post("/admin/grant-access")
async def admin_grant_access(
    request: Request,
    user_id: int = Form(...),
    redirect_tier: str = Form("all"),
    redirect_q: str = Form(""),
    redirect_page: int = Form(1),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_subscribed = True
        db.commit()
        log.info("Admin granted access to %s", user.email)
    params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
    return RedirectResponse(url=f"/admin{params}", status_code=303)


@app.post("/admin/sync-stripe-user")
async def admin_sync_stripe_user(
    request: Request,
    email: str = Form(...),
    redirect_tier: str = Form("all"),
    redirect_q: str = Form(""),
    redirect_page: int = Form(1),
    db: Session = Depends(get_db),
):
    """
    Look up a user's Stripe subscriptions by email and sync their access state.

    Handles the case where a user completed a Stripe checkout but our DB
    flag is stale (webhook missed, multiple customers created, etc.).

    Also accepts Bearer JWT auth so it can be called from the CLI.
    """
    # Auth: admin session OR Bearer JWT
    authorized = _is_admin(request)
    if not authorized:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            from web.auth import decode_access_token as _dat
            payload = _dat(auth_header[7:].strip())
            if payload and payload.get("email"):
                authorized = True
    if not authorized:
        return JSONResponse({"status": "error", "detail": "Unauthorized"}, status_code=403)

    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user:
        if _is_admin(request):
            params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
            return RedirectResponse(url=f"/admin{params}&error=user_not_found", status_code=303)
        return JSONResponse({"status": "error", "detail": f"No user found for {email}"}, status_code=404)

    # Search Stripe for all customers with this email
    best_sub = None
    best_customer_id = None
    try:
        customers = _stripe.Customer.search(query=f'email:"{user.email}"', limit=10)
        for cust in customers.auto_paging_iter():
            subs = _stripe.Subscription.list(customer=cust.id, status="all", limit=10)
            for sub in subs.auto_paging_iter():
                if sub.status in ("active", "trialing", "past_due"):
                    # Prefer the most recently created subscription
                    if best_sub is None or sub.created > best_sub.created:
                        best_sub = sub
                        best_customer_id = cust.id
    except Exception as _exc:
        log.error("admin_sync_stripe_user: Stripe search failed for %s: %s", email, _exc)
        if _is_admin(request):
            params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
            return RedirectResponse(url=f"/admin{params}", status_code=303)
        return JSONResponse({"status": "error", "detail": str(_exc)}, status_code=500)

    if best_sub:
        trial_end_ts = getattr(best_sub, "trial_end", None)
        trial_ends_at = (
            datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
            if trial_end_ts else None
        )
        user.is_subscribed            = True
        user.stripe_subscription_id   = best_sub.id
        user.stripe_customer_id       = best_customer_id
        if trial_ends_at is not None:
            user.trial_ends_at = trial_ends_at
        db.commit()
        log.info(
            "admin_sync_stripe_user: synced %s → sub=%s status=%s trial_ends=%s",
            user.email, best_sub.id, best_sub.status, trial_ends_at,
        )
        result = {
            "status": "synced", "email": user.email,
            "sub_id": best_sub.id, "sub_status": best_sub.status,
            "trial_ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
        }
    else:
        result = {"status": "no_active_sub", "email": user.email,
                  "detail": "No active/trialing Stripe subscription found for this email."}
        log.warning("admin_sync_stripe_user: no active sub found for %s", user.email)

    if _is_admin(request):
        params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
        return RedirectResponse(url=f"/admin{params}", status_code=303)
    return JSONResponse(result)


@app.post("/admin/revoke-access")
async def admin_revoke_access(
    request: Request,
    user_id: int = Form(...),
    redirect_tier: str = Form("all"),
    redirect_q: str = Form(""),
    redirect_page: int = Form(1),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_subscribed = False
        db.commit()
        log.info("Admin revoked access from %s", user.email)
    params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
    return RedirectResponse(url=f"/admin{params}", status_code=303)


@app.post("/admin/grant-trial")
async def admin_grant_trial(
    request: Request,
    user_id: int = Form(...),
    days: int = Form(7),
    redirect_tier: str = Form("all"),
    redirect_q: str = Form(""),
    redirect_page: int = Form(1),
    db: Session = Depends(get_db),
):
    """
    Grant or extend access for a user.

    days=0  → indefinite comp (clears trial_ends_at, sets is_subscribed=True)
    days>0  → timed comp (sets trial_ends_at = now + days, sets is_subscribed=True)
    """
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        if days == 0:
            user.trial_ends_at = None       # indefinite — no expiry
            user.is_subscribed = True
            log.info("Admin granted indefinite comp access to %s", user.email)
        else:
            from datetime import timedelta as _td
            new_end = datetime.now(timezone.utc) + _td(days=days)
            user.trial_ends_at = new_end
            user.is_subscribed = True
            log.info("Admin granted %d-day access to %s (ends %s)", days, user.email, new_end.date())
        db.commit()
    params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
    return RedirectResponse(url=f"/admin{params}", status_code=303)


@app.post("/admin/add-pick")
async def admin_add_pick(
    request:    Request,
    pick_date:  str   = Form(...),   # YYYY-MM-DD
    game:       str   = Form(""),
    team:       str   = Form(""),
    league:     str   = Form(""),    # sport key e.g. "baseball_mlb"
    market:     str   = Form("h2h"),
    point:      str   = Form(""),    # optional float as string
    book:       str   = Form(""),
    odds:       int   = Form(...),
    ev_percent: float = Form(0.0),
    result:     str   = Form("pending"),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    """Manually add or backfill a daily pick entry."""
    from datetime import date as _date
    try:
        pd = _date.fromisoformat(pick_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    if result not in {"won", "lost", "push", "pending"}:
        result = "pending"
    league_val = league.strip() or None
    point_val = float(point) if point.strip() else None
    existing = db.query(DailyPick).filter(DailyPick.pick_date == pd).first()
    if existing:
        # Update existing row rather than error
        existing.game       = game or existing.game
        existing.team       = team or existing.team
        existing.league     = league_val or existing.league  # update league if provided
        existing.market     = market or existing.market
        existing.point      = point_val if point_val is not None else existing.point
        existing.book       = book or existing.book
        existing.odds       = odds
        existing.ev_percent = ev_percent
        existing.result     = result
        db.commit()
        log.info("Admin updated pick for %s (league=%s)", pd, existing.league)
    else:
        db.add(DailyPick(
            pick_date  = pd,
            game       = game,
            team       = team,
            league     = league_val,
            market     = market,
            point      = point_val,
            book       = book,
            odds       = odds,
            ev_percent = ev_percent,
            result     = result,
            sent_at    = datetime.now(timezone.utc),
        ))
        db.commit()
        log.info("Admin added pick for %s (league=%s)", pd, league_val)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/pick-result")
async def admin_update_pick_result(
    request: Request,
    pick_id: int = Form(...),
    result:  str = Form(...),
    league:  str = Form(""),   # optional — lets admin fix wrong sport assignment
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    """Update the result (and optionally the sport/league) of a daily pick."""
    if result not in {"won", "lost", "push", "pending"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid result '{result}'")
    pick = db.query(DailyPick).filter(DailyPick.id == pick_id).first()
    if not pick:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pick not found")
    pick.result = result
    if league.strip():                 # only update league when admin explicitly chose one
        pick.league = league.strip()
    db.commit()
    log.info("Admin updated pick %d (%s — %s) result → %s, league → %s",
             pick_id, pick.pick_date, pick.team, result, pick.league)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/newsletter-unsubscribe")
async def admin_newsletter_unsubscribe(
    request: Request,
    subscriber_id: int = Form(...),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    sub = db.query(NewsletterSubscriber).filter(NewsletterSubscriber.id == subscriber_id).first()
    if sub:
        sub.is_active = False
        db.commit()
        log.info("Admin unsubscribed newsletter subscriber %s", sub.email)
        try:
            bh_remove(sub.email)
        except Exception as exc:
            log.error("Beehiiv remove failed for %s: %s", sub.email, exc)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/beehiiv-sync")
async def admin_beehiiv_sync(
    request: Request,
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    """
    One-time bulk sync: push all active NewsletterSubscribers into Beehiiv.
    Safe to run repeatedly — Beehiiv deduplicates by email.
    """
    active_subs = (
        db.query(NewsletterSubscriber)
        .filter(NewsletterSubscriber.is_active.is_(True))
        .all()
    )
    emails = [s.email for s in active_subs]
    result = bh_bulk_sync(emails)
    log.info("Admin Beehiiv bulk sync: %s", result)
    return JSONResponse(result)


@app.post("/admin/newsletter-resubscribe")
async def admin_newsletter_resubscribe(
    request: Request,
    subscriber_id: int = Form(...),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    sub = db.query(NewsletterSubscriber).filter(NewsletterSubscriber.id == subscriber_id).first()
    if sub:
        sub.is_active = True
        db.commit()
        log.info("Admin resubscribed newsletter subscriber %s", sub.email)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/refresh-cache")
async def admin_refresh_cache(
    request: Request,
    pin: str = Form(default=""),
):
    """
    Manually trigger an immediate EV cache refresh.

    Accepts either:
      1. Admin PIN in form field: pin=ADMIN_PIN
      2. Session-based admin auth (logged-in via /admin)
    """
    _admin_pin = os.getenv("ADMIN_PIN", "")
    authed = (
        bool(request.session.get("admin_authenticated"))
        or (pin and _admin_pin and pin == _admin_pin)
    )
    if not authed:
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    job = scheduler.get_job("ev_cache_refresh")
    # Detect whether this is a browser form POST (wants a redirect) vs AJAX (wants JSON)
    accept = request.headers.get("accept", "")
    wants_html = "text/html" in accept

    if job:
        scheduler.modify_job("ev_cache_refresh", next_run_time=datetime.now(timezone.utc))
        log.info("Manual cache refresh triggered via admin panel.")
        if wants_html:
            return RedirectResponse(url="/admin?msg=refresh_queued", status_code=303)
        return JSONResponse({"status": "refresh queued"})

    if wants_html:
        return RedirectResponse(url="/admin?error=scheduler_not_found", status_code=303)
    return JSONResponse({"status": "error", "detail": "Scheduler job not found"}, status_code=500)


@app.post("/admin/send-correction-newsletter")
async def admin_send_correction_newsletter(
    request: Request,
    pin: str = Form(default=""),
):
    """
    Send a correction newsletter email to all active subscribers.

    Use this when the scheduled 8 AM pick was wrong (stale/incorrect game).
    The email includes an apology banner and today's real top +EV pick.

    Accepts one of two auth methods:
      1. Form field: pin=ADMIN_PIN
      2. HTTP header: Authorization: Bearer <valid-JWT>

    Call with Bearer token:
        curl -X POST https://www.posit-ev.com/admin/send-correction-newsletter \\
             -H "Authorization: Bearer YOUR_JWT"
    Call with PIN:
        curl -X POST https://www.posit-ev.com/admin/send-correction-newsletter \\
             -d "pin=YOUR_PIN"
    """
    from web.auth import decode_access_token

    authorized = False

    # Method 1: valid JWT Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        payload = decode_access_token(token)
        if payload and payload.get("email"):
            authorized = True
            log.info(
                "Correction newsletter: authorized via Bearer JWT for %s",
                payload.get("email"),
            )

    # Method 2: admin PIN form field
    if not authorized and pin:
        admin_pin = os.getenv("ADMIN_PIN", "")
        if admin_pin and secrets.compare_digest(pin.strip(), admin_pin.strip()):
            authorized = True
            log.info("Correction newsletter: authorized via ADMIN_PIN")
        else:
            log.warning(
                "Correction newsletter: rejected bad PIN from %s",
                request.client.host if request.client else "unknown",
            )

    if not authorized:
        return JSONResponse(
            {"status": "error", "detail": "Unauthorized — provide a valid Bearer token or ADMIN_PIN."},
            status_code=403,
        )

    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, send_correction_newsletter)
    log.info("Correction newsletter triggered by admin: %s", result)
    return JSONResponse({"status": "sent", "result": result})
