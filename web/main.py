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

import csv
import json
import logging
import math
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from scipy import stats as scipy_stats

import stripe as _stripe
import sentry_sdk
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Path setup — allow imports from project root
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db.database import DailyPick, EVBetCache, GameSimulation, NewsletterSubscriber, OddsHistory, SessionLocal, User, WatchlistEntry, create_tables, ensure_columns  # noqa: E402
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


def _is_admin_jwt(token_payload: dict) -> bool:
    """Return True if a decoded JWT belongs to the configured admin email."""
    admin_email = os.getenv("ADMIN_EMAIL", "")
    email = (token_payload.get("email") or "").strip().lower()
    return bool(email and admin_email and email == admin_email.strip().lower())


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
            except _stripe_sync.error.InvalidRequestError as exc:
                db.rollback()
                # Stale test-mode customer ID used against live key (or deleted customer).
                # Clear it so this user stops generating errors on every sync cycle.
                log.warning(
                    "stripe_sync: clearing stale stripe_customer_id for %s: %s",
                    user.email, exc,
                )
                try:
                    user.stripe_customer_id = None
                    db.commit()
                except Exception as _dbe:
                    db.rollback()
                    log.error("stripe_sync: failed to clear stale customer_id for %s: %s", user.email, _dbe)
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

    # Credit brake — halt if monthly or daily budget is exceeded
    from scripts.odds_fetcher import credit_brake_check as _credit_brake
    if not _credit_brake("ev_cache_refresh"):
        log.warning("EV cache refresh blocked by credit brake.")
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

        # Championship futures are now fetched once daily at 3 AM CT by
        # _fetch_futures_daily() — removed from 30-min cycle to save ~92 credits/day.
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

        # ── Sharp signals setup ───────────────────────────────────────────────
        from scripts.sharp_signals import (
            compute_rlm, compute_steam, compute_line_shop_bps,
            compute_clv_grade, compute_pred_mkt, compute_sharp_grade,
        )
        from scripts.espn_fetcher import fetch_injuries_for_sport, get_injury_alert
        # Pre-fetch ESPN injury reports once per sport (avoids N+1 HTTP calls per game)
        _injury_caches: dict = {}

        # ── Row helper functions — defined once here, used inside the loop ───
        def _safe_proj_float(val):
            try:
                v = float(val)
                return None if (v != v) else v
            except (TypeError, ValueError):
                return None

        def _safe_proj_str(val):
            s = str(val) if val is not None else ""
            return s if s not in ("", "nan", "None") else None

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

            # Second-pass guard: skip quarter-point soccer spread/total lines (x.25 / x.75).
            # Asian handicap and goal-line quarter-lines are not standard in US markets.
            # get_odds_df() already filters these upstream; this ensures they can never
            # reach EVBetCache even if a pipeline run straddles a deployment window.
            _row_sport = str(row.get("sport_key", "") or "")
            _row_market = str(row.get("market", "") or "")
            if (
                _row_sport.startswith("soccer_")
                and _row_market in ("spreads", "totals")
                and point_val is not None
                and point_val % 0.5 != 0
            ):
                continue

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

            # ── Sharp signals ─────────────────────────────────────────────────
            _ev_pct_val   = float(row.get("effective_ev_pct", row.get("ev_pct", 0)) or 0)
            _true_prob_val = float(row.get("true_prob", 0) or 0)
            _all_book_json = _safe_proj_str(row.get("all_book_odds"))

            _rlm, _rlm_note         = compute_rlm(_bet_pct, opening_odds_val, odds_val)
            _steam_bps, _           = compute_steam(opening_odds_val, odds_val)
            _lshop_bps              = compute_line_shop_bps(_all_book_json, odds_val, row_book)
            _clv_grade              = compute_clv_grade(_ev_pct_val)
            _pred_aligned, _pred_note = compute_pred_mkt(_all_book_json, _true_prob_val)
            _sharp_grade            = compute_sharp_grade(
                _ev_pct_val, _rlm, _steam_bps, _lshop_bps, _pred_aligned, _clv_grade
            )

            # ESPN injury alert — fetched once per sport, reused per game row
            _sport_key = str(row.get("sport_key", "") or "")
            if _sport_key not in _injury_caches:
                try:
                    _injury_caches[_sport_key] = fetch_injuries_for_sport(_sport_key)
                except Exception:
                    _injury_caches[_sport_key] = []
            _game_str   = str(row.get("game", "") or "")
            _away_team  = _game_str.split(" @ ")[0].strip() if " @ " in _game_str else ""
            _home_team  = _game_str.split(" @ ")[1].strip() if " @ " in _game_str else ""
            _inj_alert  = get_injury_alert(
                _home_team, _away_team, _sport_key,
                injury_cache=_injury_caches[_sport_key],
            )

            # ── Attach projection snapshot (pre-fetched by report_generator) ──
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
                all_book_odds = _all_book_json,
                # Sharp signals
                rlm              = _rlm,
                rlm_note         = _rlm_note,
                steam_bps        = _steam_bps if _steam_bps > 0 else None,
                line_shop_bps    = _lshop_bps if _lshop_bps > 0 else None,
                clv_grade        = _clv_grade,
                sharp_grade      = _sharp_grade,
                pred_mkt_note    = _pred_note,
                pred_mkt_aligned = _pred_aligned,
                injury_alert     = _inj_alert,
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

        # Score HR props immediately so the HR Model tab is never empty.
        # Wind data isn't enriched yet (that runs 1 min later), so the first
        # pass uses wind_factor=1.0; the scheduled job at +4 min refines it.
        try:
            _score_hr_props()
        except Exception as _hr_exc:
            log.warning("EV cache refresh: inline HR scoring failed: %s", _hr_exc)

        # ── Smart interval — slow down off-peak to save credits ───────────
        # Peak 10 AM–1 AM CT: 30 min. Off-peak 1–10 AM CT: 90 min.
        try:
            from pytz import timezone as _tz
            _now_ct = datetime.now(_tz("America/Chicago"))
            _hour = _now_ct.hour
            _interval = 30 if (_hour >= 10 or _hour == 0) else 90
            scheduler.reschedule_job(
                "ev_cache_refresh",
                trigger=IntervalTrigger(minutes=_interval),
            )
            log.info("EV cache: next refresh in %d min (hour=%d CT)", _interval, _hour)
        except Exception as _rs_exc:
            log.warning("EV cache: could not reschedule interval: %s", _rs_exc)

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
# Game context enrichment — runs in background after each cache refresh
# ---------------------------------------------------------------------------

def _parse_game_teams(game: str) -> tuple[str, str]:
    """
    Parse 'Away Team @ Home Team' format.
    Returns (home_team, away_team).  Both empty strings on failure.
    """
    if not game:
        return "", ""
    m = re.match(r"^(.+?)\s+@\s+(.+)$", game.strip())
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", ""


def _key_injuries_changed(old_json: str, new_json: str) -> bool:
    """
    Return True if the set of Out/Doubtful players changed between two
    game_context JSON strings (signals summary should be regenerated).
    """
    try:
        def _key_set(raw: str) -> set:
            ctx = json.loads(raw)
            return {
                (inj["player"], inj["status"])
                for side in ("home", "away")
                for inj in ctx.get("injuries", {}).get(side, [])
                if inj.get("status") in ("Out", "Doubtful")
            }
        return _key_set(old_json) != _key_set(new_json)
    except Exception:
        return False


def _enrich_game_contexts() -> None:
    """
    Fetch real-world context (injuries, rest, weather, pace) for all active
    +EV bets and persist to the game_context column.

    Groups bets by game so each unique matchup is only queried once per run.
    If key injuries changed since the last run, card_summary is cleared so it
    will be regenerated with fresh context by _generate_pending_summaries().
    """
    try:
        from models.game_context import enrich_game as _enrich
    except ImportError as exc:
        log.warning("_enrich_game_contexts: import failed: %s", exc)
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        bets = (
            db.query(EVBetCache)
            .filter(EVBetCache.commence_time > now)
            .all()
        )
        if not bets:
            return

        game_ctx_cache: dict[str, dict] = {}
        updated = 0

        for bet in bets:
            game_key = bet.game_id or bet.game or f"{bet.league}:{bet.team}"

            if game_key in game_ctx_cache:
                new_ctx = game_ctx_cache[game_key]
            else:
                home, away = _parse_game_teams(bet.game or "")
                new_ctx = _enrich(
                    league=bet.league or "",
                    home_team=home,
                    away_team=away,
                    commence_time=bet.commence_time,
                )
                game_ctx_cache[game_key] = new_ctx

            if not new_ctx:
                continue

            new_ctx_json = json.dumps(new_ctx, default=str)

            # If key injuries changed → clear summary so it regenerates
            if bet.game_context and _key_injuries_changed(bet.game_context, new_ctx_json):
                bet.card_summary = None
                log.info(
                    "game_context: injury change for %r — card_summary cleared", bet.game
                )

            bet.game_context = new_ctx_json
            updated += 1

        if updated:
            try:
                db.commit()
                log.info(
                    "game_context: enriched %d bets (%d unique games)",
                    updated, len(game_ctx_cache),
                )
            except Exception as dbe:
                db.rollback()
                log.error("game_context: DB commit failed: %s", dbe)
    except Exception as exc:
        log.error("_enrich_game_contexts: unexpected error: %s", exc, exc_info=True)
    finally:
        db.close()


def _score_hr_props() -> None:
    """
    Run the MLB home run model against all upcoming batter_home_runs props.
    Scheduled 4 minutes after each cache refresh so game_context (weather) is
    already populated when the HR model runs.
    """
    from db.database import get_db as _get_db
    try:
        from models.mlb_hr_model import enrich_hr_props
    except Exception as exc:
        log.warning("_score_hr_props: import failed: %s", exc)
        return

    _db = next(_get_db())
    try:
        n = enrich_hr_props(_db)
        log.info("_score_hr_props: scored %d HR props", n)
    except Exception as exc:
        log.error("_score_hr_props: error: %s", exc, exc_info=True)
    finally:
        _db.close()


# ---------------------------------------------------------------------------
# Monte Carlo simulation runner — runs in background after each cache refresh
# ---------------------------------------------------------------------------

def _run_simulations() -> None:
    """
    For every unique MLB/soccer game in EVBetCache, run 10,000 Monte Carlo
    simulations and upsert results into the GameSimulation table.

    Uses Poisson run/goal distributions calibrated against the no-vig true
    probabilities already stored in EVBetCache (derived from The Odds API
    sharp-book consensus).  An AI summary is generated for each game using
    claude-haiku-4-5.
    """
    from models.simulator import run_simulation, SUPPORTED_SPORT_KEYS, SOCCER_SPORT_KEYS, MLB_SPORT_KEY
    import json
    import anthropic as _anthropic

    _now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            # Fetch all non-prop, non-expired rows for supported sports.
            # Use != True so NULL rows (is_prop not set) are included alongside False.
            rows = (
                db.query(EVBetCache)
                .filter(
                    EVBetCache.league.in_(list(SUPPORTED_SPORT_KEYS)),
                    EVBetCache.is_prop != True,  # noqa: E712 — catches NULL + False
                    EVBetCache.game.isnot(None),
                    (EVBetCache.commence_time == None) | (EVBetCache.commence_time > _now),  # noqa: E711
                )
                .all()
            )

            # Group by (league, game)
            games: dict[str, list] = {}
            for row in rows:
                key = f"{row.league}|{row.game}"
                games.setdefault(key, []).append(row)

            log.info("_run_simulations: found %d unique games across %d rows", len(games), len(rows))

            _client = _anthropic.Anthropic()

            for game_key, game_rows in games.items():
                try:
                    _process_one_simulation(db, game_rows, _client, SOCCER_SPORT_KEYS, MLB_SPORT_KEY, json)
                except Exception:
                    log.exception("Simulation failed for game_key=%r", game_key)

            db.commit()
            log.info("_run_simulations: complete")

    except Exception:
        log.exception("_run_simulations: outer error")


def _process_one_simulation(db, game_rows: list, client, soccer_keys, mlb_key, json_mod) -> None:
    """Compute and upsert a simulation for one game's worth of EVBetCache rows."""
    from models.simulator import run_simulation

    sport_key = game_rows[0].league
    game_str  = game_rows[0].game

    parts = (game_str or "").split(" @ ", 1)
    if len(parts) != 2:
        return
    away_team, home_team = parts[0].strip(), parts[1].strip()

    # ── Extract win probabilities from h2h rows ──────────────────────────
    h2h_rows = [r for r in game_rows if r.market == "h2h"]
    if not h2h_rows:
        return

    draw_rows  = [r for r in h2h_rows if r.team.lower() in ("draw", "tie")]
    team_rows  = [r for r in h2h_rows if r.team.lower() not in ("draw", "tie")]

    # Average true_prob across all books for each team name
    team_prob_map: dict[str, list[float]] = {}
    for r in team_rows:
        team_prob_map.setdefault(r.team, []).append(r.true_prob)
    avg_prob = {name: sum(probs) / len(probs) for name, probs in team_prob_map.items()}

    away_lc = away_team.lower()
    home_lc = home_team.lower()
    home_prob = away_prob = None
    for name, prob in avg_prob.items():
        name_lc = name.lower()
        if home_lc and (home_lc in name_lc or name_lc in home_lc):
            home_prob = prob
        elif away_lc and (away_lc in name_lc or name_lc in away_lc):
            away_prob = prob

    # Fallback: first two entries ordered as away, home (Odds API convention)
    if home_prob is None or away_prob is None:
        ordered = list(avg_prob.values())
        if len(ordered) >= 2:
            away_prob, home_prob = ordered[0], ordered[1]
        elif len(ordered) == 1:
            home_prob = ordered[0]
            away_prob = 1.0 - home_prob
        else:
            return  # cannot determine probs

    draw_prob = (sum(r.true_prob for r in draw_rows) / len(draw_rows)) if draw_rows else 0.0

    # ── Get totals line ────────────────────────────────────────────────────
    total_rows = [r for r in game_rows if r.market == "totals"]
    total_line = total_rows[0].point if total_rows else None

    # ── Run simulation ─────────────────────────────────────────────────────
    result = run_simulation(
        sport_key=sport_key,
        game=game_str,
        home_team=home_team,
        away_team=away_team,
        home_win_prob=float(home_prob),
        away_win_prob=float(away_prob),
        draw_prob=float(draw_prob),
        total_line=total_line,
        n_sims=10_000,
    )
    if result is None:
        return

    # ── Generate AI summary ────────────────────────────────────────────────
    summary = _generate_sim_summary(result, client)

    # ── Upsert into GameSimulation ─────────────────────────────────────────
    existing = db.query(GameSimulation).filter(
        GameSimulation.sport_key == sport_key,
        GameSimulation.game == game_str,
    ).first()

    sim_json = json_mod.dumps(result.narrative_data)
    now = datetime.now(timezone.utc)

    if existing:
        existing.home_win_pct      = result.home_win_pct
        existing.away_win_pct      = result.away_win_pct
        existing.draw_pct          = result.draw_pct
        existing.projected_outcome = result.projected_outcome
        existing.confidence        = result.confidence
        existing.avg_home_score    = result.avg_home_score
        existing.avg_away_score    = result.avg_away_score
        existing.summary           = summary
        existing.sim_data          = sim_json
        existing.updated_at        = now
    else:
        db.add(GameSimulation(
            sport_key         = sport_key,
            game_id           = game_rows[0].game_id,
            game              = game_str,
            home_team         = home_team,
            away_team         = away_team,
            commence_time     = game_rows[0].commence_time,
            n_sims            = result.n_sims,
            home_win_pct      = result.home_win_pct,
            away_win_pct      = result.away_win_pct,
            draw_pct          = result.draw_pct,
            projected_outcome = result.projected_outcome,
            confidence        = result.confidence,
            avg_home_score    = result.avg_home_score,
            avg_away_score    = result.avg_away_score,
            summary           = summary,
            sim_data          = sim_json,
            updated_at        = now,
            created_at        = now,
        ))
    log.info("Simulation upserted: %r — %s (%.1f%%)", game_str, result.projected_outcome, result.confidence)


def _generate_sim_summary(result, client) -> str:
    """Generate a 3-4 sentence plain-English summary of simulation results via Claude Haiku."""
    from models.simulator import MLB_SPORT_KEY, SOCCER_SPORT_KEYS

    is_soccer  = result.sport_key in SOCCER_SPORT_KEYS
    sport_lbl  = "soccer match" if is_soccer else "baseball game"
    score_unit = "goals" if is_soccer else "runs"
    nd         = result.narrative_data

    draw_line = (
        f"Draw: {result.draw_pct:.1f}% of simulations\n"
        if is_soccer else ""
    )
    ou_line = ""
    if nd.get("total_line") and nd.get("over_pct") is not None:
        ou_line = f"Over/Under {nd['total_line']}: {nd['over_pct']:.0f}% Over / {nd.get('under_pct', 0):.0f}% Under\n"

    context = f"""Game: {result.game} ({sport_lbl})
Simulations run: {result.n_sims:,}

Simulation Results:
{result.home_team} wins: {result.home_win_pct:.1f}% of simulations
{draw_line}{result.away_team} wins: {result.away_win_pct:.1f}% of simulations
{ou_line}
Projected Outcome: {result.projected_outcome} ({result.confidence:.1f}% confidence)
Expected Score: {result.home_team} {result.avg_home_score:.1f} — {result.away_team} {result.avg_away_score:.1f} {score_unit}

Market-Implied Probabilities (sharp no-vig):
{result.home_team}: {nd.get('market_home_win_prob', '?')}%
{"Draw: " + str(nd.get('market_draw_prob', '?')) + "%" if is_soccer else ""}
{result.away_team}: {nd.get('market_away_win_prob', '?')}%"""

    prompt = (
        "You are a data-driven sports analyst. Based on the Monte Carlo simulation "
        "results below, write exactly 3 sentences explaining why the projected outcome "
        "is likely. Reference the confidence percentage and expected scoring. "
        "Be direct and specific — no hedging, no filler phrases.\n\n"
        + context
    )

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=180,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception:
        log.exception("_generate_sim_summary: Claude call failed")
        return ""


# ---------------------------------------------------------------------------
# Card summary generation — runs in background after each cache refresh
# ---------------------------------------------------------------------------

def _generate_pending_summaries() -> None:
    """
    For every EVBetCache row that has no card_summary yet, generate one using
    claude-haiku-4-5 via generate_card_summary().  Runs in a background thread
    after refresh_ev_cache() so it never blocks the scheduler or request cycle.
    """
    try:
        from models.ai_analyzer import generate_card_summary as _gen_summary
    except ImportError as exc:
        log.warning("_generate_pending_summaries: import failed: %s", exc)
        return

    db = SessionLocal()
    try:
        pending = (
            db.query(EVBetCache)
            .filter(EVBetCache.card_summary.is_(None))
            .all()
        )
        if not pending:
            return
        log.info("Card summaries: generating for %d picks…", len(pending))
        generated = 0
        for row in pending:
            bet_dict = {
                "id":               row.id,
                "game":             row.game or "",
                "league":           row.league or "",
                "market":           row.market or "",
                "team":             row.team or "",
                "odds":             row.odds or 0,
                "true_prob":        row.true_prob or 0.5,
                "ev_percent":       row.ev_percent or 0.0,
                "implied_prob":     row.implied_prob,
                "point":            row.point,
                "player_name":      row.player_name,
                "is_prop":          bool(row.is_prop),
                "opening_odds":     row.opening_odds,
                "bet_pct":          row.bet_pct,
                "money_pct":        row.money_pct,
                "sharp_score":      row.sharp_score,
                "home_trend":       row.home_trend or "",
                "away_trend":       row.away_trend or "",
                "proj_home_win_prob": row.proj_home_win_prob,
                "proj_total":       row.proj_total,
                "adj_flags":        row.adj_flags or "",
                "game_context":     row.game_context,   # real-world enrichment JSON
            }
            summary = _gen_summary(bet_dict)
            if summary:
                try:
                    row.card_summary = summary
                    db.commit()
                    generated += 1
                except Exception as _dbe:
                    db.rollback()
                    log.warning("Card summaries: DB write failed for id=%d: %s", row.id, _dbe)
        log.info("Card summaries: generated %d/%d.", generated, len(pending))
    except Exception as exc:
        log.error("_generate_pending_summaries: unexpected error: %s", exc, exc_info=True)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    create_tables()
    ensure_columns()  # Add any new columns missing from production DB
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

    # Migrate card_summary in its own block so a failure here never rolls back
    # the main migration batch above.
    with SessionLocal() as _db:
        try:
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS card_summary TEXT"))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate sharp-signal columns (added for multi-signal conviction system)
    with SessionLocal() as _db:
        try:
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS game_context TEXT"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS rlm BOOLEAN DEFAULT FALSE"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS rlm_note VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS steam_bps INTEGER"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS line_shop_bps INTEGER"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS clv_grade VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS sharp_grade VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS pred_mkt_note VARCHAR"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS pred_mkt_aligned BOOLEAN DEFAULT FALSE"))
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS injury_alert TEXT"))
            _db.commit()
        except Exception:
            _db.rollback()

    # Migrate game_context (real-world enrichment JSON) — isolated block.
    with SessionLocal() as _db:
        try:
            _db.execute(text("ALTER TABLE ev_bet_cache ADD COLUMN IF NOT EXISTS game_context TEXT"))
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

    # ── Credit housekeeping helpers ───────────────────────────────────────────

    def _reset_daily_credits():
        try:
            from scripts.odds_fetcher import reset_daily_credits as _rdc
            _rdc()
        except Exception as _e:
            log.warning("Daily credit reset failed: %s", _e)

    def _reset_monthly_credits():
        try:
            from scripts.odds_fetcher import reset_monthly_credits as _rmc
            _rmc()
        except Exception as _e:
            log.warning("Monthly credit reset failed: %s", _e)

    def _fetch_futures_daily():
        """
        Fetch championship futures once per day (3 AM CT) and merge into EVBetCache.
        Replaces the per-30-min futures call to save ~92 credits/day.
        """
        try:
            from scripts.odds_fetcher import fetch_futures_only as _ff
            from models.ev_calculator import find_all_positive_ev as _fev
            import pandas as _pd
            futures_df = _ff()
            if futures_df is None or futures_df.empty:
                return
            futures_ev_df = _fev(futures_df, markets=["outrights"])
            if futures_ev_df.empty:
                return
            futures_ev_df["is_prop"] = False
            db = SessionLocal()
            try:
                from db.database import EVBetCache
                existing_ids = {r.game_id for r in db.query(EVBetCache.game_id).all() if r.game_id}
                rows = []
                for _, row in futures_ev_df.iterrows():
                    gid = str(row.get("game_id", "") or "")
                    if gid in existing_ids:
                        continue
                    rows.append(EVBetCache(
                        game_id=gid,
                        league=str(row.get("sport_key", "")),
                        market=str(row.get("market", "outrights")),
                        team=str(row.get("outcome_name", "")),
                        game=str(row.get("game", "")),
                        book=str(row.get("bookmaker", "")),
                        odds=int(row.get("price", -110)),
                        ev_percent=float(row.get("ev_percent", 0)),
                        true_prob=float(row.get("true_prob", 0)),
                    ))
                if rows:
                    db.add_all(rows)
                    db.commit()
                    log.info("Futures daily job: inserted %d futures bets.", len(rows))
            finally:
                db.close()
        except Exception as exc:
            log.warning("_fetch_futures_daily failed: %s", exc)

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
    # Monte Carlo simulations — runs 3 min after each cache refresh.
    # Fires after enrichment so adjusted_prob values are available.
    scheduler.add_job(
        _run_simulations,
        trigger=IntervalTrigger(minutes=30),
        id="game_simulations",
        name="Monte Carlo simulations for MLB/soccer (every 30 min)",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        misfire_grace_time=120,
        replace_existing=True,
    )
    # Enrich game context (injuries, rest, weather, pace) ~1 min after cache
    # refresh.  Runs before summary generation so summaries include fresh data.
    scheduler.add_job(
        _enrich_game_contexts,
        trigger=IntervalTrigger(minutes=30),
        id="game_context_enrich",
        name="Enrich game context (injuries/rest/weather/pace) every 30 min",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        misfire_grace_time=120,
        replace_existing=True,
    )
    # Generate card summaries ~2 min after each cache refresh so new picks have
    # summaries by the time users arrive.  Uses Haiku — fast and cheap.
    scheduler.add_job(
        _generate_pending_summaries,
        trigger=IntervalTrigger(minutes=30),
        id="card_summary_gen",
        name="Generate card summaries for new picks (every 30 min)",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        misfire_grace_time=120,
        replace_existing=True,
    )
    # Score HR props — runs 4 min after cache refresh so weather/context is ready
    scheduler.add_job(
        _score_hr_props,
        trigger=IntervalTrigger(minutes=30),
        id="hr_prop_scoring",
        name="Score HR props with MLB HR model (every 30 min)",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4),
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

    # Daily futures fetch — 3:02 AM CT (just after purge), once per day
    scheduler.add_job(
        _fetch_futures_daily,
        trigger=CronTrigger(hour=3, minute=2, timezone="America/Chicago"),
        id="futures_daily",
        name="Daily championship futures fetch (3:02 AM CT)",
        replace_existing=True,
    )

    # Credit counter resets
    scheduler.add_job(
        _reset_daily_credits,
        trigger=CronTrigger(hour=0, minute=0, timezone="America/Chicago"),
        id="credit_reset_daily",
        name="Reset daily Odds API credit counter (midnight CT)",
        replace_existing=True,
    )
    scheduler.add_job(
        _reset_monthly_credits,
        trigger=CronTrigger(day=1, hour=0, minute=1, timezone="America/Chicago"),
        id="credit_reset_monthly",
        name="Reset monthly Odds API credit counter (1st of month CT)",
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

class SubscriptionMiddleware:
    """
    Pure ASGI middleware that guards /dashboard and /welcome.

    Replaces BaseHTTPMiddleware to avoid Starlette's known issue where
    BaseHTTPMiddleware wraps the ASGI `receive` callable even on pass-through
    paths — this corrupts the raw request body and breaks Stripe webhook
    signature verification (construct_event gets empty bytes → 400).

    For unguarded paths (including /stripe/webhook) the original scope,
    receive, and send are passed directly to the next app — the body is
    never touched.

    1. Missing / invalid JWT            → redirect /login
    2. User not subscribed AND
       not within active trial window   → redirect /pricing
    3. Subscribed OR in active trial    → pass through

    Trial safety net: if is_subscribed is False but trial_ends_at is set
    and still in the future, the user is mid-trial and gets full access.
    The flag is healed in-place so subsequent requests skip this check.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        protected = ("/dashboard", "/welcome")
        if not any(path.startswith(p) for p in protected):
            # Unguarded path — pass through without touching receive at all.
            # This is critical for /stripe/webhook: the raw body must remain
            # intact for Stripe signature verification.
            await self.app(scope, receive, send)
            return

        # Build a request object only to read cookies/headers — no body read.
        request = StarletteRequest(scope, receive)

        async def _send_redirect(url: str) -> None:
            response = RedirectResponse(url=url, status_code=303)
            await response(scope, receive, send)

        token = get_token_from_request(request)
        if not token:
            await _send_redirect("/login")
            return

        payload = decode_access_token(token)
        if not payload:
            await _send_redirect("/login")
            return

        db: Session = SessionLocal()
        try:
            user = db.query(User).filter(User.id == int(payload["sub"])).first()
            if not user:
                await _send_redirect("/login")
                return

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
                    await _send_redirect("/pricing")
                    return
        finally:
            db.close()

        # Authorized — pass through with original receive intact.
        await self.app(scope, receive, send)


app.add_middleware(SubscriptionMiddleware)
_secret_key = os.getenv("SECRET_KEY", "")
if not _secret_key or _secret_key == "dev-secret-change-in-production":
    raise RuntimeError(
        "SECRET_KEY env var is not set or uses the insecure default. "
        "Set a strong random secret in your .env / Railway environment."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
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

    # Soccer leagues: Optimal doesn't model soccer — skip immediately rather
    # than making a live call that will always fail (and look "slow" to users).
    _SOCCER_LEAGUES = {
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_usa_mls", "soccer_uefa_champs_league", "soccer_fifa_world_cup",
    }
    if (bet_row.league or "") in _SOCCER_LEAGUES:
        raise HTTPException(status_code=422, detail="No model projection available for soccer")

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
        # This only runs for bets added after the last pipeline run (< 30 min
        # window). Cap the live call at 12 s so users aren't left hanging.
        log.info("Projection live-fetch (no pipeline data) for bet_id=%d", bet_id)
        try:
            proj, ctx = await asyncio.wait_for(
                asyncio.gather(
                    loop.run_in_executor(None, fetch_game_projections, bet_row.game, bet_row.league),
                    loop.run_in_executor(None, _fgc, bet_row.game, bet_row.league, bet_row.commence_time),
                    return_exceptions=True,
                ),
                timeout=12.0,
            )
            if isinstance(proj, Exception):
                log.error("Projection fetch error for bet_id=%d: %s", bet_id, proj)
                proj = None
            if isinstance(ctx, Exception):
                log.warning("Context fetch error for bet_id=%d: %s", bet_id, ctx)
                ctx = {}
        except asyncio.TimeoutError:
            log.warning("Projection live-fetch timed out for bet_id=%d", bet_id)
            proj = None
            ctx  = {}

        if not proj and not ctx:
            # No data at all — return 422 so the frontend shows the clean
            # "no projection" message instead of a scary error.
            raise HTTPException(
                status_code=422,
                detail="Projection not yet available — refresh after the next pipeline run"
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


@app.get("/api/simulation/{bet_id}")
async def get_simulation(bet_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Return pre-computed Monte Carlo simulation results for a bet's game.

    Subscription required.  Only available for MLB and soccer leagues.
    Results are pre-computed by the background simulation job and served
    directly from the GameSimulation table — no live computation at request time.
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

    from models.simulator import SUPPORTED_SPORT_KEYS
    if (bet_row.league or "") not in SUPPORTED_SPORT_KEYS:
        raise HTTPException(status_code=422, detail="Simulation not available for this sport")

    if not bet_row.game or " @ " not in (bet_row.game or ""):
        raise HTTPException(status_code=422, detail="No simulation for this bet type")

    sim = db.query(GameSimulation).filter(
        GameSimulation.sport_key == bet_row.league,
        GameSimulation.game == bet_row.game,
    ).first()

    if not sim:
        raise HTTPException(
            status_code=404,
            detail="Simulation not yet available — runs 3 min after next cache refresh",
        )

    import json as _json
    sim_data = {}
    if sim.sim_data:
        try:
            sim_data = _json.loads(sim.sim_data)
        except Exception:
            pass

    # Determine whether the simulation's projected winner matches the bet.
    # Uses the same word-overlap logic as the projection panel's model_agrees_with_bet.
    def _word_set_sim(s: str) -> set:
        skip = {"at", "the", "a", "an", "vs", "fc", "sc", "city", "state"}
        return {w for w in (s or "").lower().split() if w not in skip and len(w) > 2}

    sim_agrees_with_bet: Optional[bool] = None
    _bet_team = (bet_row.team or "").strip()
    _projected = (sim.projected_outcome or "")
    if _bet_team and _projected and bet_row.market in ("h2h", "h2h_3_way", "spreads"):
        # projected_outcome is e.g. "Home Win" or "Away Win" — resolve to team name
        _g_parts   = (sim.game or "").split(" @ ", 1)
        _away_sim  = _g_parts[0].strip() if len(_g_parts) > 1 else ""
        _home_sim  = _g_parts[1].strip() if len(_g_parts) > 1 else ""
        _proj_team = _home_sim if "home" in _projected.lower() else _away_sim
        if _proj_team:
            sim_agrees_with_bet = bool(
                _word_set_sim(_proj_team) & _word_set_sim(_bet_team)
            )

    return JSONResponse({
        "sport_key":            sim.sport_key,
        "game":                 sim.game,
        "home_team":            sim.home_team,
        "away_team":            sim.away_team,
        "n_sims":               sim.n_sims,
        "projected_outcome":    sim.projected_outcome,
        "confidence":           sim.confidence,
        "home_win_pct":         sim.home_win_pct,
        "away_win_pct":         sim.away_win_pct,
        "draw_pct":             sim.draw_pct,
        "avg_home_score":       sim.avg_home_score,
        "avg_away_score":       sim.avg_away_score,
        "total_line":           sim_data.get("total_line"),
        "over_pct":             sim_data.get("over_pct"),
        "under_pct":            sim_data.get("under_pct"),
        "market_home_win_prob": sim_data.get("market_home_win_prob"),
        "market_away_win_prob": sim_data.get("market_away_win_prob"),
        "market_draw_prob":     sim_data.get("market_draw_prob"),
        "summary":              sim.summary,
        "updated_at":           sim.updated_at.isoformat() if sim.updated_at else None,
        "sim_agrees_with_bet":  sim_agrees_with_bet,
    })


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

    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    _auth_payload = decode_access_token(token)
    if not _auth_payload or not _auth_payload.get("sub"):
        raise HTTPException(status_code=401, detail="Authentication required")

    bet_row = db.query(EVBetCache).filter(EVBetCache.id == bet_id).first()
    if not bet_row:
        raise HTTPException(status_code=404, detail="Bet not found")

    # Return cached analysis if fresh (< 6 hours old) and not force-busted.
    # Both Claude and Gemini results are cached — the 6h window prevents
    # redundant AI calls on bets that sit on the dashboard all day.
    import re as _re
    bust = request.query_params.get("bust") == "1"
    cache_cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    if (
        not bust
        and bet_row.analysis
        and bet_row.analysis_generated_at
        and bet_row.analysis_generated_at > cache_cutoff
    ):
        # Extract recommended_action from stored text "**Strong Bet**\n\n..."
        _m   = _re.match(r"^\*\*([^*]+)\*\*", bet_row.analysis or "")
        _rec = _m.group(1) if _m else ""
        _cgc: dict = {}
        try:
            import json as _json
            _cgc = _json.loads(bet_row.game_context or "{}") or {}
        except Exception:
            pass
        return JSONResponse({
            "analysis":             bet_row.analysis,
            "confidence_score":     bet_row.confidence_score,
            "kelly_pct":            bet_row.kelly_pct,
            "cached":               True,
            "rule_based":           False,
            "edge_tag":             "",
            "recommended_action":   _rec,
            "home_record":          _cgc.get("home_record", ""),
            "away_record":          _cgc.get("away_record", ""),
            "home_streak":          _cgc.get("home_streak", ""),
            "away_streak":          _cgc.get("away_streak", ""),
            "home_team":            _cgc.get("home_team", ""),
            "away_team":            _cgc.get("away_team", ""),
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
        # Stored real-world enrichment (injuries, rest/B2B, weather, pace)
        "game_context":     bet_row.game_context or "",
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

    rule_based = False
    if result is None:
        # AI service unavailable (e.g. credit balance depleted) —
        # fall back to rule-based analysis derived from pipeline data.
        from models.ai_analyzer import rule_based_analyze_bet
        result     = rule_based_analyze_bet(bet_dict)
        rule_based = True
        log.info("get_analysis: using rule-based fallback for bet_id=%d", bet_id)

    # Persist Claude and Gemini results to DB so the 6h cache window is honoured.
    # Rule-based results are excluded — they're instant to regenerate and we don't
    # want a no-context fallback blocking a real AI result later.
    if not rule_based:
        try:
            bet_row.analysis               = result["analysis"]
            bet_row.analysis_generated_at  = datetime.now(timezone.utc)
            bet_row.confidence_score       = result["confidence_score"]
            bet_row.kelly_pct              = result["kelly_pct"]
            db.commit()
        except Exception as exc:
            log.warning("Failed to cache analysis for bet_id=%d: %s", bet_id, exc)
            db.rollback()

    _rec = (result.get("raw") or {}).get("analysis", {}).get("recommended_action", "") or ""
    # Parse game_context for team records/streaks and include in response
    _gc: dict = {}
    try:
        import json as _json
        _gc = _json.loads(bet_dict.get("game_context") or "{}") or {}
    except Exception:
        pass
    return JSONResponse({
        "analysis":             result["analysis"],
        "confidence_score":     result["confidence_score"],
        "kelly_pct":            result["kelly_pct"],
        "edge_tag":             result.get("edge_tag", ""),
        "cached":               False,
        "rule_based":           rule_based,
        "recommended_action":   _rec,
        "home_record":          _gc.get("home_record", ""),
        "away_record":          _gc.get("away_record", ""),
        "home_streak":          _gc.get("home_streak", ""),
        "away_streak":          _gc.get("away_streak", ""),
        "home_team":            _gc.get("home_team", ""),
        "away_team":            _gc.get("away_team", ""),
    })


def _compute_pick_streak(settled_picks) -> int:
    """
    Count consecutive wins from the most recent settled pick.

    `settled_picks` must be ordered descending by pick_date (most recent first).
    Returns the length of the current win streak; 0 if the latest pick was not a win.
    """
    streak = 0
    for pick in settled_picks:
        if pick.result == "won":
            streak += 1
        else:
            break
    return streak


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

    # P&L and ROI based on $20 flat unit size, $1,000 bankroll
    # ROI = net profit / bankroll × 100  (matches admin dashboard formula)
    UNIT, BANKROLL = 20.0, 1000.0
    total_pl = 0.0
    for p in settled:
        if not p.odds:
            continue
        if p.result == "won":
            total_pl += UNIT * p.odds / 100 if p.odds > 0 else UNIT * 100 / abs(p.odds)
        elif p.result == "lost":
            total_pl -= UNIT
        # push = 0 net
    total_pl    = round(total_pl, 2)
    total_units = round(total_pl / UNIT, 2)

    # ROI: profit relative to starting bankroll (same as admin dashboard)
    track_roi = round(total_pl / BANKROLL * 100, 1) if len(settled) > 0 else None

    # Current win streak (consecutive wins from most recent pick)
    streak_count = _compute_pick_streak(settled)

    # Chart data: cumulative ROI over time (ascending by date), for the stock graph.
    # Start at 0.0 before the first pick so the curve visibly "launches" from zero.
    import json as _json
    _chart_picks = sorted(settled, key=lambda p: p.pick_date)
    _cum_pl = 0.0
    _chart_points = [{"date": "Start", "roi": 0.0, "pl": 0.0}]
    for _p in _chart_picks:
        if _p.result == "won" and _p.odds:
            _cum_pl += UNIT * _p.odds / 100 if _p.odds > 0 else UNIT * 100 / abs(_p.odds)
        elif _p.result == "lost":
            _cum_pl -= UNIT
        _chart_points.append({
            "date": _p.pick_date.strftime("%-m/%-d"),
            "roi":  round(_cum_pl / BANKROLL * 100, 2),
            "pl":   round(_cum_pl, 2),
        })
    track_chart_data = _json.dumps(_chart_points)

    # Top 3 live picks for the hero mockup — real data replaces hardcoded cards
    _hero_now = datetime.now(timezone.utc)
    hero_picks = (
        db.query(EVBetCache)
        .filter(
            (EVBetCache.commence_time == None) |  # noqa: E711
            (EVBetCache.commence_time > _hero_now)
        )
        .order_by(EVBetCache.ev_percent.desc())
        .limit(3)
        .all()
    )

    return templates.TemplateResponse(request, "index.html", {
        "track_picks":      settled,
        "track_won":        won,
        "track_lost":       lost,
        "track_total":      len(settled),
        "track_roi":        track_roi,
        "track_pl":         total_pl,
        "track_units":      total_units,
        "streak_count":     streak_count,
        "track_chart_data": track_chart_data,
        "hero_picks":       hero_picks,
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
            # Heal stale is_subscribed when Stripe confirms the subscription is gone.
            # This happens when a subscription is cancelled outside the app (e.g. via
            # Stripe dashboard) and the webhook event was missed or delayed.
            if sub_status in ("canceled", "incomplete_expired") and user.is_subscribed:
                user.is_subscribed = False
                db.commit()
                log.info(
                    "account_page: healed is_subscribed=False for %s "
                    "(Stripe status=%s, sub=%s)",
                    user.email, sub_status, user.stripe_subscription_id,
                )
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
        "reactivate_error":     qs.get("reactivate_error"),
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
    """Immediately cancel the Stripe subscription — access ends right away."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

    # If stripe_subscription_id is missing, look it up from Stripe using the
    # customer ID — covers webhook-miss cases where the field was never written.
    if not user.stripe_subscription_id and user.stripe_customer_id:
        try:
            subs_list = list(_stripe.Subscription.list(
                customer=user.stripe_customer_id,
                status="all",
                limit=10,
            ).auto_paging_iter())
            active = next(
                (s for s in subs_list
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
            elif any(s.status in ("canceled", "incomplete_expired") for s in subs_list):
                # All subscriptions are already cancelled — heal the DB and treat as success.
                log.warning(
                    "cancel_subscription: all Stripe subs are already cancelled for user %s"
                    " — healing DB.",
                    user.email,
                )
                try:
                    user.is_subscribed = False
                    user.trial_ends_at = None
                    db.commit()
                except Exception as _db_exc:
                    log.error("cancel_subscription: DB heal failed: %s", _db_exc)
                return RedirectResponse(url="/account?cancel_done=1", status_code=303)
        except _stripe.error.InvalidRequestError as exc:
            # Stale customer ID (e.g. test-mode ID in live mode) — clear it so
            # future requests don't keep hitting the same dead end.
            log.warning(
                "cancel_subscription: invalid customer_id %s for %s — clearing from DB: %s",
                user.stripe_customer_id, user.email, exc,
            )
            user.stripe_customer_id = None
            try:
                db.commit()
            except Exception:
                pass
        except _stripe.error.StripeError as exc:
            log.error("cancel_subscription: Stripe lookup failed for user %s: %s", user.email, exc)

    if not user.stripe_subscription_id:
        # No Stripe subscription ID in DB. Two cases:
        # 1. Admin-granted trial — no Stripe sub exists at all, just clear DB access.
        # 2. Stripe-backed trial with a webhook miss — sub exists but wasn't stored.
        # For case 1, clearing access is the entire "cancel". For case 2, the
        # Stripe sub will expire naturally or be caught by the next sync.
        _has_access = user.is_subscribed or (
            getattr(user, "trial_ends_at", None)
            and user.trial_ends_at > datetime.now(timezone.utc)
        )
        if _has_access:
            log.info(
                "cancel_subscription: no stripe_subscription_id for %s — "
                "clearing DB access directly (admin trial or webhook miss).",
                user.email,
            )
            user.is_subscribed = False
            user.trial_ends_at = None
            try:
                db.commit()
            except Exception as _dbe:
                log.error("cancel_subscription: DB clear failed: %s", _dbe)
            return RedirectResponse(url="/account?cancel_done=1", status_code=303)
        return RedirectResponse(url="/account?cancel_error=no_sub", status_code=303)

    try:
        # Immediately cancel — no grace period, no end-of-cycle access.
        # Stripe fires customer.subscription.deleted; webhook also sets is_subscribed=False.
        _stripe.Subscription.cancel(user.stripe_subscription_id)
        log.info("Subscription cancelled immediately for user %s", user.email)
    except _stripe.error.InvalidRequestError as exc:
        # Any InvalidRequestError (already cancelled, expired, no such subscription,
        # incomplete_expired, etc.) means the subscription is effectively gone in Stripe.
        # The user's intent is to cancel — always clear DB access and treat as success
        # regardless of the specific Stripe error message.
        err_code = (getattr(exc, "code", "") or "").lower()
        log.warning(
            "cancel_subscription: InvalidRequestError for sub %s user %s (code=%s): %s "
            "— clearing DB access and treating as success.",
            user.stripe_subscription_id, user.email, err_code, exc,
        )
        user.is_subscribed = False
        user.trial_ends_at = None
        try:
            db.commit()
        except Exception as _db_exc:
            log.error("cancel_subscription: DB clear failed: %s", _db_exc)
        return RedirectResponse(url="/account?cancel_done=1", status_code=303)
    except _stripe.error.StripeError as exc:
        # Any Stripe error (auth failure, network outage, rate limit, etc.) while
        # the user is explicitly requesting cancellation should NOT block them.
        # Clear DB access immediately and log prominently for manual Stripe follow-up.
        # The subscription may still be active in Stripe — operations team should
        # verify via the Stripe Dashboard if needed.
        log.error(
            "Cancel subscription StripeError for user %s sub %s: %s "
            "— clearing DB access anyway (user intent = cancel).",
            user.email, user.stripe_subscription_id, exc, exc_info=True,
        )
        user.is_subscribed = False
        user.trial_ends_at = None
        try:
            db.commit()
        except Exception as _db_exc:
            log.error("cancel_subscription: DB clear failed after StripeError: %s", _db_exc)
        return RedirectResponse(url="/account?cancel_done=1", status_code=303)
    except Exception as exc:
        log.error(
            "Cancel subscription unexpected error for user %s sub %s: %s "
            "— clearing DB access anyway.",
            user.email, user.stripe_subscription_id, exc, exc_info=True,
        )
        user.is_subscribed = False
        user.trial_ends_at = None
        try:
            db.commit()
        except Exception as _db_exc:
            log.error("cancel_subscription: DB clear failed after Exception: %s", _db_exc)
        return RedirectResponse(url="/account?cancel_done=1", status_code=303)

    # Stripe call succeeded — revoke access in DB immediately (don't wait for webhook)
    user.is_subscribed = False
    user.trial_ends_at = None
    try:
        db.commit()
    except Exception as exc:
        log.error("Cancel subscription: DB commit failed for user %s: %s", user.email, exc)

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
    except Exception as exc:
        log.error("Reactivate subscription failed for user %s: %s", user.email, exc, exc_info=True)
        return RedirectResponse(url="/account?reactivate_error=1", status_code=303)

    return RedirectResponse(url="/account?reactivated=1", status_code=303)


@app.get("/account/billing-portal")
async def billing_portal(request: Request, db: Session = Depends(get_db)):
    """Redirect to Stripe Customer Portal for payment method / invoice management."""
    user = _get_authed_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not _stripe.api_key:
        log.error("billing_portal: STRIPE_SECRET_KEY not configured.")
        return RedirectResponse(url="/account?portal_error=1", status_code=303)

    base_url = os.getenv("BASE_URL", "http://localhost:8000")

    def _ensure_live_customer() -> "str | None":
        """
        Return a valid live-mode Stripe customer ID for this user.
        Creates one if missing or stale. Returns None on failure.
        """
        if user.stripe_customer_id:
            # Verify it exists in the current key's mode.
            try:
                _stripe.Customer.retrieve(user.stripe_customer_id)
                return user.stripe_customer_id          # valid — use it
            except _stripe.error.InvalidRequestError:
                log.warning(
                    "billing_portal: stale customer_id %s for %s — will create fresh",
                    user.stripe_customer_id, user.email,
                )
                user.stripe_customer_id = None
                try:
                    db.commit()
                except Exception:
                    db.rollback()

        # Create a fresh customer in the current key's mode.
        try:
            cust = _stripe.Customer.create(
                email=user.email,
                metadata={"user_id": str(user.id)},
            )
            user.stripe_customer_id = cust.id
            db.commit()
            log.info(
                "billing_portal: created customer %s for %s",
                cust.id, user.email,
            )
            return cust.id
        except _stripe.error.StripeError as exc:
            log.error("billing_portal: failed to create customer for %s: %s", user.email, exc)
            return None

    customer_id = _ensure_live_customer()
    if not customer_id:
        return RedirectResponse(url="/account?portal_error=1", status_code=303)

    log.info("billing_portal: opening portal for %s customer=%s", user.email, customer_id)
    try:
        portal = _stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base_url}/account",
        )
        return RedirectResponse(url=portal.url, status_code=303)
    except _stripe.error.StripeError as exc:
        log.error("billing_portal: portal session failed for %s: %s", user.email, exc)
        return RedirectResponse(url="/account?portal_error=1", status_code=303)
    except Exception as exc:
        log.error("billing_portal: unexpected error for %s: %s", user.email, exc, exc_info=True)
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
    # Only show bets for games that:
    #   (a) have no commence_time (rare edge case, included so they're never silently dropped), OR
    #   (b) start on today's date (CT) or later AND haven't started yet.
    # Two-part filter prevents both stale yesterday rows and already-started games
    # from appearing even if a cache refresh hasn't run recently.
    _now_utc = datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo as _ZI
    _CT2 = _ZI("America/Chicago")
    _today_ct = datetime.now(_CT2).date()
    _today_midnight_utc = datetime(
        _today_ct.year, _today_ct.month, _today_ct.day,
        tzinfo=_CT2
    ).astimezone(timezone.utc)
    bets = (
        db.query(EVBetCache)
        .filter(
            (EVBetCache.commence_time == None) |  # noqa: E711
            (
                (EVBetCache.commence_time >= _today_midnight_utc) &
                (EVBetCache.commence_time > _now_utc)
            )
        )
        .order_by(EVBetCache.ev_percent.desc())
        .all()
    )

    # Remove S-grade picks where the line has moved against the position.
    # opening_odds < odds  →  CLV is negative  →  S grade is contradicted.
    _before = len(bets)
    bets = [
        b for b in bets
        if not (
            b.sharp_grade == "S"
            and b.opening_odds
            and b.opening_odds != b.odds
            and b.opening_odds < b.odds
        )
    ]
    if len(bets) < _before:
        log.info("dashboard: dropped %d S-grade / neg-CLV bets", _before - len(bets))

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

    # HR model picks — top scored batter_home_runs props for the HR Model tab
    import json as _json
    hr_picks = []
    try:
        hr_bets = (
            db.query(EVBetCache)
            .filter(
                EVBetCache.market == "batter_home_runs",
                EVBetCache.hr_model_score != None,  # noqa: E711
                EVBetCache.hr_model_score > 0,
                (EVBetCache.commence_time == None) |  # noqa: E711
                (EVBetCache.commence_time > _now_utc),
            )
            .order_by(EVBetCache.hr_model_score.desc())
            .limit(10)
            .all()
        )
        for b in hr_bets:
            meta = _json.loads(b.hr_model_meta) if b.hr_model_meta else {}
            imp  = float(b.implied_prob or 0)
            if imp > 1.0:
                imp /= 100.0
            hr_picks.append({
                "id":           b.id,
                "player_name":  b.player_name or "",
                "game":         b.game or "",
                "odds":         b.odds,
                "ev_percent":   round(float(b.ev_percent or 0), 1),
                "hr_model_prob": round(float(b.hr_model_prob or 0), 4),
                "implied_prob": round(imp, 4),
                "hr_model_score": round(float(b.hr_model_score or 0), 1),
                "edge_pp":      round((float(b.hr_model_prob or 0) - imp) * 100, 1),
                "pitcher_name": meta.get("pitcher_name", "TBA"),
                "pitcher_hr9":  meta.get("pitcher_hr9", 1.3),
                "park_label":   meta.get("park_label", "Neutral park"),
                "park_factor":  meta.get("park_factor", 1.0),
                "wind_label":   meta.get("wind_label", "Wind N/A"),
                "batter_hr_ppa": meta.get("batter_hr_ppa", 0.035),
                "batter_pa":    meta.get("batter_pa", 0),
                "analysis":     meta.get("analysis", ""),
                "commence_time": b.commence_time,
                "book":         b.book,
                "opening_odds": b.opening_odds,
            })
    except Exception as _exc:
        log.warning("dashboard: hr_picks fetch failed: %s", _exc)

    # Build 3-4 case-for-the-pick bullets for the featured banner
    fp_bullets: list[str] = []
    try:
        if bets:
            _fb = bets[0]
            _gc: dict = {}
            try:
                _gc = _json.loads(_fb.game_context or "{}") or {}
            except Exception:
                pass

            # If game_context is empty, do a quick ESPN fetch for the top pick now.
            # This ensures season records + streaks are always available even if the
            # enrichment scheduler hasn't run yet today.
            if not _gc.get("home_record") and _fb.game and " @ " in _fb.game:
                try:
                    from scripts.context_fetcher import fetch_game_context as _fgc
                    _live_gc = _fgc(_fb.game, _fb.league or "", _fb.commence_time)
                    if _live_gc:
                        _gc = _live_gc
                        # Also persist it so subsequent loads are instant
                        try:
                            _fb.game_context = _json.dumps(_live_gc)
                            db.commit()
                        except Exception:
                            db.rollback()
                except Exception as _gc_err:
                    log.debug("fp_bullets: on-demand game_context fetch failed: %s", _gc_err)

            # Determine home/away
            _game_str = _fb.game or ""
            _parts = _game_str.split(" @ ", 1) if " @ " in _game_str else []
            _away_name = _parts[0].strip() if _parts else ""
            _home_name = _parts[1].strip() if len(_parts) > 1 else ""

            import re as _re2

            def _parse_trend(s: str):
                """Parse '7-3 W4', '7-3 L2', or bare '7-3'. Returns (wins,losses,dir,streak) or None."""
                s = (s or "").strip()
                if not s:
                    return None
                # Full form: "7-3 W4"
                m = _re2.match(r"^(\d+)-(\d+)\s+([WL])(\d+)$", s)
                if m:
                    return int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
                # Short form: "7-3" (streak unknown/absent)
                m = _re2.match(r"^(\d+)-(\d+)$", s)
                if m:
                    w, l = int(m.group(1)), int(m.group(2))
                    return w, l, "W" if w >= l else "L", 0
                return None

            def _streak_sentence(team: str, wins: int, losses: int, dir_: str, streak: int, ha: str = "") -> str:
                ha_txt = f" ({ha})" if ha else ""
                if dir_ == "W" and streak >= 3:
                    return f"{team}{ha_txt} on a {streak}-game win streak ({wins}-{losses} L10)."
                elif dir_ == "W" and streak >= 1:
                    return f"{team}{ha_txt} have won {streak} straight ({wins}-{losses} L10)."
                elif dir_ == "L" and streak >= 3:
                    return f"{team}{ha_txt} have lost {streak} in a row ({wins}-{losses} L10)."
                else:
                    return f"{team}{ha_txt} are {wins}-{losses} over their last 10 games."

            # ── 1. Season records from game_context (ESPN) ────────────────
            # These come from the enrichment scheduler and are the most
            # reliable source for current-season records + live streaks.
            _home_rec  = _gc.get("home_record", "")
            _home_strk = _gc.get("home_streak", "")  # e.g. "W3", "L2"
            _away_rec  = _gc.get("away_record", "")
            _away_strk = _gc.get("away_streak", "")

            def _streak_from_espn(strk: str) -> str:
                """'W3' → '3-game win streak', 'L2' → '2-game losing streak'."""
                if not strk:
                    return ""
                m = _re2.match(r"^([WL])(\d+)$", strk)
                if not m:
                    return strk
                d, n = m.group(1), int(m.group(2))
                if n >= 2:
                    return f"{n}-game {'win' if d=='W' else 'losing'} streak"
                return f"{'won' if d=='W' else 'lost'} last game"

            if _home_rec:
                _hs_txt = _streak_from_espn(_home_strk)
                _hs_suffix = f", {_hs_txt}" if _hs_txt else ""
                fp_bullets.append(
                    f"{_home_name or 'Home team'} (home) are {_home_rec} on the season{_hs_suffix}."
                )

            if _away_rec:
                _as_txt = _streak_from_espn(_away_strk)
                _as_suffix = f", {_as_txt}" if _as_txt else ""
                fp_bullets.append(
                    f"{_away_name or 'Away team'} (away) are {_away_rec} on the season{_as_suffix}."
                )

            # ── 2. L10 trend from pipeline (home_trend / away_trend) ──────
            # Supplement if game_context didn't supply records for both sides.
            for _tname, _tval, _tha in [
                (_home_name or "Home team", _fb.home_trend, "home"),
                (_away_name or "Away team", _fb.away_trend, "away"),
            ]:
                if not _tval or len(fp_bullets) >= 3:
                    continue
                _parsed = _parse_trend(_tval)
                if _parsed:
                    _tw, _tl, _td, _ts = _parsed
                    fp_bullets.append(_streak_sentence(_tname, _tw, _tl, _td, _ts, _tha))

            # ── 3. Sharp money / public split ─────────────────────────────
            _mp = _fb.money_pct
            _bp = _fb.bet_pct
            _ss = _fb.sharp_score
            if _mp is not None and float(_mp) >= 55 and len(fp_bullets) < 4:
                _rev = _bp is not None and float(_mp) > float(_bp) + 8
                if _rev:
                    fp_bullets.append(
                        f"Reverse line movement: {int(_bp)}% of bets but "
                        f"{int(_mp)}% of the money is on {_fb.team}."
                    )
                else:
                    fp_bullets.append(
                        f"{int(_mp)}% of sharp betting money has come in on "
                        f"{_fb.team} today."
                    )
            elif _ss is not None and float(_ss) >= 60 and len(fp_bullets) < 4:
                fp_bullets.append(
                    f"Sharp score {int(_ss)}/100 — professional bettors are "
                    f"aligned with this pick."
                )

            # ── 4. Model edge — always the last bullet ────────────────────
            _ev = float(_fb.ev_percent or 0)
            _true_p = float(_fb.true_prob or 0.5) * 100
            _raw_odds = int(_fb.odds or -110)
            _impl_p = (100 / (_raw_odds + 100) * 100) if _raw_odds > 0 \
                      else (abs(_raw_odds) / (abs(_raw_odds) + 100) * 100)
            _odds_str = f"+{_raw_odds}" if _raw_odds > 0 else str(_raw_odds)
            fp_bullets.append(
                f"Model gives a {_true_p:.0f}% true probability vs. the book's "
                f"implied {_impl_p:.0f}% — a +{_ev:.1f}% EV edge at {_odds_str}."
            )

            fp_bullets = fp_bullets[:4]
    except Exception as _fbe:
        log.warning("dashboard: fp_bullets build failed: %s", _fbe)

    # Build sim lookup: {game_str: GameSimulation} for supported sports
    from models.simulator import SUPPORTED_SPORT_KEYS as _SIM_SPORTS
    try:
        _sim_rows = (
            db.query(GameSimulation)
            .filter(
                GameSimulation.sport_key.in_(list(_SIM_SPORTS)),
                (GameSimulation.commence_time == None) | (GameSimulation.commence_time > _now_utc),  # noqa: E711
            )
            .all()
        )
        game_sim_map = {gs.game: gs for gs in _sim_rows}
    except Exception as _sim_err:
        log.warning("dashboard: game_sim_map build failed: %s", _sim_err)
        game_sim_map = {}

    # ── get_line_history: read past odds from daily CSV reports ───────────────
    _REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'reports')

    def get_line_history(game_id, market, team):
        try:
            results = []
            if not os.path.isdir(_REPORTS_DIR):
                return []
            for fname in sorted(os.listdir(_REPORTS_DIR)):
                if not fname.startswith('ev_report_') or not fname.endswith('.csv'):
                    continue
                date_part = fname[len('ev_report_'):-len('.csv')]
                fpath = os.path.join(_REPORTS_DIR, fname)
                try:
                    with open(fpath, newline='', encoding='utf-8') as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            if row.get('game_id') != game_id:
                                continue
                            if row.get('market') != market:
                                continue
                            outcome = (row.get('outcome_name') or '').lower()
                            if team.lower() not in outcome:
                                continue
                            try:
                                odds_val = int(float(row.get('american_odds', 0)))
                                ev_val = float(row.get('ev_pct', row.get('ev_percent', 0)) or 0)
                                results.append({'date': date_part, 'odds': odds_val, 'ev_pct': ev_val})
                            except (ValueError, TypeError):
                                continue
                except Exception:
                    continue
            results = sorted(results, key=lambda x: x['date'])[-10:]
            if len(results) < 2:
                return []
            return results
        except Exception:
            return []

    # ── Computed signal fields attached to each bet ───────────────────────────
    def _bet_signals(b):
        # 1. consensus_score (0-100): weighted average of available signals
        sigs = {}
        if b.ev_percent is not None:
            sigs['ev'] = min(100.0, float(b.ev_percent) / 20.0 * 100.0)
        if b.true_prob is not None and b.implied_prob is not None:
            _tp = float(b.true_prob); _ip = float(b.implied_prob)
            if _tp > 1.0: _tp /= 100.0
            if _ip > 1.0: _ip /= 100.0
            sigs['prob_gap'] = min(100.0, max(0.0, (_tp - _ip) * 500.0))
        _gm = {'S': 100.0, 'A': 80.0, 'B': 60.0, 'C': 40.0}
        if b.sharp_grade in _gm:
            sigs['sharp'] = _gm[b.sharp_grade]
        if b.opening_odds and b.opening_odds != b.odds:
            sigs['clv'] = 100.0 if b.opening_odds > b.odds else 0.0
        else:
            sigs['clv'] = 50.0
        try:
            _gs = game_sim_map.get(b.game or '')
            if _gs:
                for _attr in ('home_win_prob', 'win_prob', 'sim_win_prob'):
                    _mv = getattr(_gs, _attr, None)
                    if _mv is not None:
                        sigs['mc'] = float(_mv) * 100.0
                        break
        except Exception:
            pass
        _wts = {'ev': 0.30, 'prob_gap': 0.25, 'sharp': 0.20, 'clv': 0.15, 'mc': 0.10}
        _tw = sum(_wts[k] for k in sigs if k in _wts)
        _raw = (sum(sigs[k] * _wts[k] for k in sigs if k in _wts) / _tw) if _tw else 50.0
        b.consensus_score = max(0, min(100, int(round(_raw))))

        # 2. value_expiry_minutes: base by market, reduced by sharp grade / CLV / time-to-game
        _mkt = b.market or ''
        if _mkt in ('h2h', 'h2h_3_way', 'btts'):
            _base = 180
        elif _mkt == 'spreads':
            _base = 120
        elif _mkt in ('totals', 'team_totals', 'nrfi'):
            _base = 240
        else:
            _base = 90
        _mult = 1.0
        if b.sharp_grade in ('S', 'A'):
            _mult *= 0.70
        if b.opening_odds and b.opening_odds != b.odds and b.opening_odds < b.odds:
            _mult *= 0.80
        if b.commence_time:
            try:
                _ct = b.commence_time if b.commence_time.tzinfo else b.commence_time.replace(tzinfo=timezone.utc)
                if (_ct - _now_utc).total_seconds() < 7200:
                    _mult *= 0.50
            except Exception:
                pass
        b.value_expiry_minutes = max(15, int(_base * _mult))

        # 3. divergence_score: abs pp gap between model true prob and book implied prob
        if b.true_prob is not None and b.implied_prob is not None:
            _tp = float(b.true_prob); _ip = float(b.implied_prob)
            if _tp > 1.0: _tp /= 100.0
            if _ip > 1.0: _ip /= 100.0
            b.divergence_score = round(abs(_tp - _ip) * 100.0, 1)
        else:
            b.divergence_score = 0.0

        # 4. sharp_book_heatmap: sharp vs soft book avg implied probs from all_book_odds
        _SHARP = {'pinnacle', 'betcris', 'bookmaker', 'circa', 'betonlineag'}
        _SOFT  = {'draftkings', 'fanduel', 'betmgm', 'caesars', 'pointsbet', 'bet365'}
        try:
            _raw = b.all_book_odds
            if isinstance(_raw, str):
                _raw = _json.loads(_raw)
            _ao = _raw if isinstance(_raw, dict) else {}
        except Exception:
            _ao = {}
        # 5. consensus_line, consensus_implied_prob, book_deviation, deviation_flag
        try:
            _raw_abo = b.all_book_odds
            if isinstance(_raw_abo, str):
                import json as _json
                _raw_abo = _json.loads(_raw_abo)
            _ao2 = _raw_abo if isinstance(_raw_abo, dict) else {}
            if _ao2:
                _odds_vals = [float(v) for v in _ao2.values() if v is not None]
                if _odds_vals:
                    b.consensus_line = int(round(sum(_odds_vals) / len(_odds_vals)))
                    _probs = []
                    for _o2 in _odds_vals:
                        if _o2 > 0:
                            _probs.append(100.0 / (_o2 + 100.0))
                        else:
                            _probs.append(abs(_o2) / (abs(_o2) + 100.0))
                    b.consensus_implied_prob = round(sum(_probs) / len(_probs), 4)
                    b.book_deviation = int(b.odds) - b.consensus_line
                    if b.book_deviation >= 3:
                        b.deviation_flag = 'above'
                    elif b.book_deviation <= -3:
                        b.deviation_flag = 'below'
                    else:
                        b.deviation_flag = 'consensus'
                else:
                    b.consensus_line = None; b.consensus_implied_prob = None
                    b.book_deviation = 0; b.deviation_flag = 'consensus'
            else:
                b.consensus_line = None; b.consensus_implied_prob = None
                b.book_deviation = 0; b.deviation_flag = 'consensus'
        except Exception:
            b.consensus_line = None; b.consensus_implied_prob = None
            b.book_deviation = 0; b.deviation_flag = 'consensus'

        # 6. money_bet_gap, sharp_divergence_signal, fade_signal
        if b.money_pct is not None and b.bet_pct is not None:
            b.money_bet_gap = round(abs(float(b.money_pct) - float(b.bet_pct)), 1)
            b.sharp_divergence_signal = float(b.money_pct) > float(b.bet_pct) + 15
            b.fade_signal = float(b.bet_pct) > float(b.money_pct) + 20
        else:
            b.money_bet_gap = None
            b.sharp_divergence_signal = False
            b.fade_signal = False

        _sp, _fp, _sn, _fn = [], [], [], []
        for _bk_k, _ov in _ao.items():
            try:
                _o = float(_ov)
                _pr = (100.0 / (_o + 100.0)) if _o > 0 else (abs(_o) / (abs(_o) + 100.0))
            except (TypeError, ZeroDivisionError, ValueError):
                continue
            _bl = str(_bk_k).lower()
            if any(_s in _bl for _s in _SHARP):
                _sp.append(_pr); _sn.append(_bk_k)
            elif any(_s in _bl for _s in _SOFT):
                _fp.append(_pr); _fn.append(_bk_k)
        if _sp and _fp:
            _sa = sum(_sp) / len(_sp); _fa = sum(_fp) / len(_fp)
            b.sharp_book_heatmap = {
                'sharp_avg_prob': round(_sa, 4),
                'soft_avg_prob':  round(_fa, 4),
                'divergence':     round((_sa - _fa) * 100.0, 1),
                'sharp_books_present': _sn,
                'soft_books_present':  _fn,
            }
        else:
            b.sharp_book_heatmap = None

    for _sb in bets:
        try:
            _bet_signals(_sb)
        except Exception as _bse:
            log.debug("dashboard: _bet_signals failed bet %s: %s", getattr(_sb, 'id', '?'), _bse)
            _sb.consensus_score          = 50
            _sb.value_expiry_minutes     = 120
            _sb.divergence_score         = 0.0
            _sb.sharp_book_heatmap       = None
            _sb.consensus_line           = None
            _sb.consensus_implied_prob   = None
            _sb.book_deviation           = 0
            _sb.deviation_flag           = 'consensus'
            _sb.money_bet_gap            = None
            _sb.sharp_divergence_signal  = False
            _sb.fade_signal              = False
        # Attach line history for sparklines
        try:
            _lh = get_line_history(
                _sb.game_id or '',
                _sb.market or '',
                _sb.team or '',
            )
            _sb.line_history = json.dumps(_lh)
        except Exception:
            _sb.line_history = '[]'
        # Odds freshness indicator
        try:
            _ts = getattr(_sb, 'odds_updated_at', None) or getattr(_sb, 'created_at', None)
            if _ts:
                _ts_aware = _ts if _ts.tzinfo else _ts.replace(tzinfo=timezone.utc)
                _sb.odds_age_minutes = max(0, int((_now_utc - _ts_aware).total_seconds() / 60))
            else:
                _sb.odds_age_minutes = 999
        except Exception:
            _sb.odds_age_minutes = 999

    # ── find_middles: surface same-game opposite-leg middle opportunities ─────
    def find_middles(bets_list):
        try:
            from collections import defaultdict
            by_key = defaultdict(list)
            for b in bets_list:
                mkt = b.market or ''
                if mkt not in ('spreads', 'totals'):
                    continue
                gid = b.game_id or ''
                by_key[(gid, mkt)].append(b)

            middles_out = []
            for (gid, mkt), group in by_key.items():
                if len(group) < 2:
                    continue
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        b1, b2 = group[i], group[j]
                        try:
                            if mkt == 'spreads':
                                t1 = (b1.team or '').lower()
                                t2 = (b2.team or '').lower()
                                if t1 == t2:
                                    continue
                                p1 = float(b1.point or 0)
                                p2 = float(b2.point or 0)
                                # A middle exists if both spreads favor the same-side push window
                                # e.g., Team A -3 and Team B -4 → window of 1pt
                                gap = abs(abs(p1) - abs(p2))
                                if gap < 1.0:
                                    continue
                                center = (abs(p1) + abs(p2)) / 2.0
                                window = gap
                                std = 10.0
                            else:  # totals
                                team1 = (b1.team or '').lower()
                                team2 = (b2.team or '').lower()
                                if 'over' in team1 and 'under' in team2:
                                    b_over, b_under = b1, b2
                                elif 'under' in team1 and 'over' in team2:
                                    b_over, b_under = b2, b1
                                else:
                                    continue
                                over_pt = float(b_over.point or 0)
                                under_pt = float(b_under.point or 0)
                                if under_pt <= over_pt:
                                    continue
                                window = under_pt - over_pt
                                center = (over_pt + under_pt) / 2.0
                                std = 8.0
                                b1, b2 = b_over, b_under

                            # Compute probabilities
                            mid_win_prob = (
                                scipy_stats.norm.cdf(center + window / 2, loc=center, scale=std)
                                - scipy_stats.norm.cdf(center - window / 2, loc=center, scale=std)
                            )
                            o1 = int(b1.odds or -110)
                            o2 = int(b2.odds or -110)
                            imp1 = (100.0 / (o1 + 100.0)) if o1 > 0 else (abs(o1) / (abs(o1) + 100.0))
                            imp2 = (100.0 / (o2 + 100.0)) if o2 > 0 else (abs(o2) / (abs(o2) + 100.0))
                            worst_case = abs(imp1 + imp2 - 2) * 50
                            pf1 = o1 / 100.0 if o1 > 0 else 100.0 / abs(o1)
                            pf2 = o2 / 100.0 if o2 > 0 else 100.0 / abs(o2)
                            best_case = (pf1 + pf2) / 2.0 * 100.0
                            combined_ev = ((b1.ev_percent or 0) + (b2.ev_percent or 0)) / 2.0

                            # Grade
                            if mid_win_prob >= 0.12 and combined_ev >= 3:
                                grade = 'Elite'
                            elif mid_win_prob >= 0.08 and combined_ev >= 2:
                                grade = 'Strong'
                            elif mid_win_prob >= 0.05:
                                grade = 'Value'
                            else:
                                grade = 'Speculative'

                            _ct = b1.commence_time
                            try:
                                game_time = _ct.strftime('%-I:%M %p') if _ct and hasattr(_ct, 'strftime') else ''
                            except Exception:
                                game_time = str(_ct)[:16] if _ct else ''

                            o1_str = f'+{o1}' if o1 > 0 else str(o1)
                            o2_str = f'+{o2}' if o2 > 0 else str(o2)

                            middles_out.append({
                                'middle_window':       round(window, 1),
                                'middle_win_prob':     round(mid_win_prob, 4),
                                'worst_case_loss_pct': round(worst_case, 1),
                                'best_case_profit_pct': round(best_case, 1),
                                'combined_ev':         round(combined_ev, 1),
                                'books_involved':      [b1.book or '', b2.book or ''],
                                'middle_grade':        grade,
                                'game':                b1.game or '',
                                'game_time':           game_time,
                                'market_display':      'Spread' if mkt == 'spreads' else 'Total',
                                'leg1_book':           b1.book or '',
                                'leg1_pick':           b1.team or '',
                                'leg1_odds':           o1,
                                'leg1_odds_str':       o1_str,
                                'leg2_book':           b2.book or '',
                                'leg2_pick':           b2.team or '',
                                'leg2_odds':           o2,
                                'leg2_odds_str':       o2_str,
                            })
                        except Exception:
                            continue

            middles_out.sort(key=lambda x: x['middle_win_prob'], reverse=True)
            return middles_out
        except Exception as _me:
            log.warning("find_middles failed: %s", _me)
            return []

    # ── get_ev_leaderboard: best EV pick per league ───────────────────────────
    def get_ev_leaderboard(bets_list):
        from collections import defaultdict
        by_league = defaultdict(list)
        for b in bets_list:
            if b.ev_percent is not None:
                by_league[b.league].append(b)
        result = []
        _league_display = {
            'americanfootball_nfl':  'NFL',
            'basketball_nba':        'NBA',
            'icehockey_nhl':         'NHL',
            'baseball_mlb':          'MLB',
            'soccer_usa_mls':        'MLS',
            'soccer':                'Soccer',
            'americanfootball_ncaaf':'NCAAF',
            'basketball_ncaab':      'NCAAB',
        }
        for league, league_bets in by_league.items():
            best = max(league_bets, key=lambda x: x.ev_percent)
            _odds = best.odds
            _odds_str = ('+' + str(_odds)) if _odds > 0 else str(_odds)
            _mkt = best.market or ''
            _mkt_display = {
                'h2h': 'Moneyline', 'spreads': 'Spread',
                'totals': 'Total', 'h2h_3_way': 'Moneyline',
            }.get(_mkt, _mkt.replace('_', ' ').title())
            _ct = best.commence_time
            _ct_disp = ''
            if _ct:
                try:
                    _ct_disp = _ct.strftime('%-I:%M %p') if hasattr(_ct, 'strftime') else str(_ct)[:16]
                except Exception:
                    _ct_disp = str(_ct)[:16]
            result.append({
                'league':                 league,
                'league_display':         _league_display.get(league, league.split('_')[-1].upper()),
                'team':                   best.team or '',
                'market_display':         _mkt_display,
                'odds_str':               _odds_str,
                'ev_percent':             float(best.ev_percent),
                'consensus_score':        getattr(best, 'consensus_score', 50),
                'sharp_grade':            best.sharp_grade or '—',
                'commence_time_display':  _ct_disp,
                'bet_id':                 best.id,
            })
        result.sort(key=lambda x: x['ev_percent'], reverse=True)
        return result[:6]

    middles      = find_middles(bets)
    ev_leaderboard = get_ev_leaderboard(bets)

    wl_pending_count = 0
    if current_user:
        try:
            wl_pending_count = db.query(WatchlistEntry).filter(
                WatchlistEntry.user_id == current_user.id,
                WatchlistEntry.paper_result == 'pending'
            ).count()
        except Exception:
            wl_pending_count = 0

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
            "hr_picks":             hr_picks,
            "game_sim_map":         game_sim_map,
            "fp_bullets":           fp_bullets,
            "middles":              middles,
            "ev_leaderboard":       ev_leaderboard,
            "wl_pending_count":     wl_pending_count,
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
# Watchlist / Paper Trail routes
# ---------------------------------------------------------------------------

@app.post('/watchlist/add')
async def watchlist_add(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    body = await request.json()
    bet_cache_id = body.get('bet_cache_id')
    try:
        bet = db.query(EVBetCache).filter(EVBetCache.id == bet_cache_id).first() if bet_cache_id else None
        entry = WatchlistEntry(
            user_id=current_user.id,
            bet_cache_id=bet_cache_id,
            game=bet.game if bet else None,
            league=bet.league if bet else None,
            team=bet.team if bet else None,
            market=bet.market if bet else None,
            odds=bet.odds if bet else None,
            ev_percent=float(bet.ev_percent) if bet and bet.ev_percent else None,
            true_prob=float(bet.true_prob) if bet and bet.true_prob else None,
            paper_result='pending',
            paper_odds=bet.odds if bet else None,
        )
        db.add(entry)
        db.commit()
        count = db.query(WatchlistEntry).filter(WatchlistEntry.user_id == current_user.id, WatchlistEntry.paper_result == 'pending').count()
        return JSONResponse({'success': True, 'count': count})
    except Exception as exc:
        db.rollback()
        import logging as _logging
        _logging.getLogger(__name__).error('watchlist_add error: %s', exc)
        return JSONResponse({'success': False, 'error': str(exc)}, status_code=500)


@app.delete('/watchlist/remove/{entry_id}')
async def watchlist_remove(entry_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse({'success': False}, status_code=401)
    entry = db.query(WatchlistEntry).filter(WatchlistEntry.id == entry_id, WatchlistEntry.user_id == current_user.id).first()
    if entry:
        db.delete(entry)
        db.commit()
    return JSONResponse({'success': True})


@app.patch('/watchlist/update/{entry_id}')
async def watchlist_update(entry_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return JSONResponse({'success': False}, status_code=401)
    body = await request.json()
    paper_result = body.get('paper_result')
    entry = db.query(WatchlistEntry).filter(WatchlistEntry.id == entry_id, WatchlistEntry.user_id == current_user.id).first()
    if entry and paper_result in ('win', 'loss', 'push', 'pending'):
        entry.paper_result = paper_result
        db.commit()
    return JSONResponse({'success': True})


@app.get('/watchlist')
async def watchlist_page(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user:
        return RedirectResponse('/login', status_code=302)
    entries = db.query(WatchlistEntry).filter(WatchlistEntry.user_id == current_user.id).order_by(WatchlistEntry.added_at.desc()).all()
    total = len(entries)
    pending = sum(1 for e in entries if e.paper_result == 'pending')
    wins = sum(1 for e in entries if e.paper_result == 'win')
    losses = sum(1 for e in entries if e.paper_result == 'loss')
    pushes = sum(1 for e in entries if e.paper_result == 'push')
    return templates.TemplateResponse('watchlist.html', {
        'request': request, 'user': current_user, 'entries': entries,
        'total': total, 'pending': pending, 'wins': wins, 'losses': losses, 'pushes': pushes,
        'wl_pending_count': pending,
    })


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
    # Tier priority (highest wins):
    #   1. Unsubscribed  — is_subscribed=False AND stripe_subscription_id IS NOT NULL
    #                      (cancelled Stripe sub beats any lingering trial_ends_at)
    #   2. Trial         — is_subscribed=True  AND active trial_ends_at
    #   3. Paid          — is_subscribed=True  AND stripe_subscription_id IS NOT NULL, no trial
    #   4. Comped        — is_subscribed=True  AND no stripe sub, no trial
    #   5. Free          — is_subscribed=False AND no stripe_subscription_id
    _active_trial_cond = (
        User.trial_ends_at.isnot(None) & (User.trial_ends_at > _now_admin)
    )
    # Unsubscribed: cancelled stripe sub, regardless of any leftover trial date
    user_unsubscribed = db.query(User).filter(
        User.is_subscribed.is_(False),
        User.stripe_subscription_id.isnot(None),
    ).count()
    # Trial: actively subscribed + trial date in future (cancelled users excluded above)
    user_trial = db.query(User).filter(
        _active_trial_cond,
        User.is_subscribed.is_(True),
    ).count()
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
    user_free  = user_total - user_paid - user_trial - user_unsubscribed

    # Build filtered query — tier filter matches the counter logic exactly
    query = db.query(User)
    if tier == "trial":
        query = query.filter(
            _active_trial_cond,
            User.is_subscribed.is_(True),
        )
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
    elif tier == "unsubscribed":
        # Cancelled stripe sub beats any leftover trial date — no trial condition
        query = query.filter(
            User.is_subscribed.is_(False),
            User.stripe_subscription_id.isnot(None),
        )
    elif tier == "free":
        query = query.filter(
            User.is_subscribed.is_(False),
            User.stripe_subscription_id.is_(None),
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
        "soccer_fifa_world_cup": "World Cup",
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
            "user_unsubscribed": user_unsubscribed,
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
        # If the user has a Stripe subscription that still exists and has a
        # pending cancellation, undo it so they continue billing normally.
        # If the subscription was already cancelled (e.g. by admin revoke or
        # self-cancel), this is comp / manual override — DB-only, no Stripe action.
        if user.stripe_subscription_id:
            try:
                _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
                sub = _stripe.Subscription.retrieve(user.stripe_subscription_id)
                if sub.status in ("active", "trialing", "past_due"):
                    if sub.cancel_at_period_end:
                        _stripe.Subscription.modify(
                            user.stripe_subscription_id,
                            cancel_at_period_end=False,
                        )
                        log.info(
                            "Admin grant: undid pending Stripe cancellation for %s (sub %s)",
                            user.email, user.stripe_subscription_id,
                        )
                    else:
                        log.info(
                            "Admin grant: Stripe sub %s for %s is already active — DB flag only.",
                            user.stripe_subscription_id, user.email,
                        )
                else:
                    # Subscription is cancelled in Stripe — granting as comp (DB only).
                    log.info(
                        "Admin grant: Stripe sub %s for %s is %s — granting comp access (DB only).",
                        user.stripe_subscription_id, user.email, sub.status,
                    )
            except Exception as _exc:
                log.warning(
                    "Admin grant: Stripe check failed for %s (continuing DB grant): %s",
                    user.email, _exc,
                )
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
    # Auth: admin session OR Bearer JWT belonging to ADMIN_EMAIL
    authorized = _is_admin(request)
    if not authorized:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            from web.auth import decode_access_token as _dat
            payload = _dat(auth_header[7:].strip())
            if payload and _is_admin_jwt(payload):
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

    # Search Stripe for all customers with this email.
    # Track the best active sub and the most recent cancelled sub separately
    # so we can heal is_subscribed in both directions.
    best_active_sub    = None
    best_cancelled_sub = None
    best_customer_id   = None
    try:
        customers = _stripe.Customer.search(query=f'email:"{user.email}"', limit=10)
        for cust in customers.auto_paging_iter():
            subs = _stripe.Subscription.list(customer=cust.id, status="all", limit=10)
            for sub in subs.auto_paging_iter():
                if sub.status in ("active", "trialing", "past_due"):
                    if best_active_sub is None or sub.created > best_active_sub.created:
                        best_active_sub  = sub
                        best_customer_id = cust.id
                elif sub.status in ("canceled", "incomplete_expired"):
                    if best_cancelled_sub is None or sub.created > best_cancelled_sub.created:
                        best_cancelled_sub = sub
                        if best_customer_id is None:
                            best_customer_id = cust.id
    except Exception as _exc:
        log.error("admin_sync_stripe_user: Stripe search failed for %s: %s", email, _exc)
        if _is_admin(request):
            params = f"?tier={redirect_tier}&q={redirect_q}&page={redirect_page}"
            return RedirectResponse(url=f"/admin{params}", status_code=303)
        return JSONResponse({"status": "error", "detail": str(_exc)}, status_code=500)

    if best_active_sub:
        # Active subscription found — ensure DB reflects access
        trial_end_ts = getattr(best_active_sub, "trial_end", None)
        trial_ends_at = (
            datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
            if trial_end_ts else None
        )
        user.is_subscribed          = True
        user.stripe_subscription_id = best_active_sub.id
        user.stripe_customer_id     = best_customer_id
        if trial_ends_at is not None:
            user.trial_ends_at = trial_ends_at
        db.commit()
        log.info(
            "admin_sync_stripe_user: synced ACTIVE %s → sub=%s status=%s trial_ends=%s",
            user.email, best_active_sub.id, best_active_sub.status, trial_ends_at,
        )
        result = {
            "status": "synced_active", "email": user.email,
            "sub_id": best_active_sub.id, "sub_status": best_active_sub.status,
            "trial_ends_at": trial_ends_at.isoformat() if trial_ends_at else None,
        }
    elif best_cancelled_sub:
        # No active sub, but a cancelled one exists — heal to Unsubscribed
        user.is_subscribed          = False
        user.trial_ends_at          = None
        user.stripe_subscription_id = best_cancelled_sub.id   # keep for Unsubscribed tab
        if best_customer_id:
            user.stripe_customer_id = best_customer_id
        db.commit()
        log.info(
            "admin_sync_stripe_user: healed CANCELLED %s → sub=%s status=%s → is_subscribed=False",
            user.email, best_cancelled_sub.id, best_cancelled_sub.status,
        )
        result = {
            "status": "synced_cancelled", "email": user.email,
            "sub_id": best_cancelled_sub.id, "sub_status": best_cancelled_sub.status,
            "detail": "Subscription is cancelled in Stripe — user moved to Unsubscribed.",
        }
    else:
        result = {"status": "no_sub_found", "email": user.email,
                  "detail": "No Stripe subscription found for this email."}
        log.warning("admin_sync_stripe_user: no subscription found for %s", user.email)

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
        # Cancel the Stripe subscription immediately so the user is not charged
        # on any future billing cycle or at the end of their trial.
        # This is non-fatal — the DB revoke below always runs regardless.
        if user.stripe_subscription_id:
            try:
                _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
                _stripe.Subscription.cancel(user.stripe_subscription_id)
                log.info(
                    "Admin revoked: cancelled Stripe sub %s for %s",
                    user.stripe_subscription_id, user.email,
                )
            except _stripe.error.InvalidRequestError as _exc:
                # Sub already cancelled in Stripe — that's fine.
                log.info(
                    "Admin revoked: Stripe sub %s for %s already cancelled: %s",
                    user.stripe_subscription_id, user.email, _exc,
                )
            except Exception as _exc:
                log.error(
                    "Admin revoked: Stripe cancel failed for %s (continuing DB revoke): %s",
                    user.email, _exc,
                )
        user.is_subscribed = False
        user.trial_ends_at = None
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


@app.post("/admin/mark-unsubscribed")
async def admin_mark_unsubscribed(
    request: Request,
    user_id: int = Form(...),
    redirect_tier: str = Form("all"),
    redirect_q: str = Form(""),
    redirect_page: int = Form(1),
    db: Session = Depends(get_db),
):
    """
    Manually move a user to the Unsubscribed tier.

    - Sets is_subscribed=False
    - Clears trial_ends_at
    - Cancels any active Stripe subscription
    - Keeps stripe_subscription_id so the user appears in the Unsubscribed tab
    """
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        # Cancel Stripe subscription if one exists and is still active
        if user.stripe_subscription_id:
            try:
                import stripe as _stripe
                _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
                sub = _stripe.Subscription.retrieve(user.stripe_subscription_id)
                if sub.status not in ("canceled", "incomplete_expired"):
                    _stripe.Subscription.cancel(user.stripe_subscription_id)
                    log.info("Admin mark-unsubscribed: cancelled Stripe sub %s for %s",
                             user.stripe_subscription_id, user.email)
            except Exception as _exc:
                log.warning("Admin mark-unsubscribed: Stripe cancel failed for %s (continuing): %s",
                            user.email, _exc)
        user.is_subscribed = False
        user.trial_ends_at = None
        db.commit()
        log.info("Admin mark-unsubscribed: moved %s to Unsubscribed tier", user.email)
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


@app.get("/admin/credit-usage")
async def admin_credit_usage(request: Request):
    """Return in-memory Odds API credit usage counters. Admin-only."""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Admin access required")
    from scripts.odds_fetcher import get_credit_summary as _gcs
    return JSONResponse(_gcs())


@app.post("/admin/run-simulations")
async def admin_run_simulations(request: Request, pin: str = Form(default="")):
    """Manually trigger the Monte Carlo simulation job."""
    _admin_pin = os.getenv("ADMIN_PIN", "")
    authed = (
        bool(request.session.get("admin_authenticated"))
        or (pin and _admin_pin and secrets.compare_digest(pin, _admin_pin))
    )
    if not authed:
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    import threading
    threading.Thread(target=_run_simulations, daemon=True).start()
    return JSONResponse({"status": "simulation job started in background"})


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
        or (pin and _admin_pin and secrets.compare_digest(pin, _admin_pin))
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

    # Method 1: valid JWT Bearer token belonging to ADMIN_EMAIL
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        payload = decode_access_token(token)
        if payload and _is_admin_jwt(payload):
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


# ---------------------------------------------------------------------------
# Admin: manually trigger the daily newsletter (missed / outage recovery)
# ---------------------------------------------------------------------------

@app.post("/admin/trigger-daily-newsletter")
async def admin_trigger_daily_newsletter(
    request: Request,
    pin: str = Form(default=""),
):
    """
    Manually fire the 8 AM daily newsletter send outside its scheduled window.

    Use when the scheduled job was missed (e.g. Railway outage, server restart
    after 8 AM CT, or Anthropic credits just topped up after a failure).

    Auth (same as other admin endpoints):
      • Form field:  pin=<ADMIN_PIN>
      • HTTP header: Authorization: Bearer <valid-JWT>

    Example:
        curl -X POST https://www.posit-ev.com/admin/trigger-daily-newsletter \\
             -d "pin=YOUR_PIN"
    """
    from web.auth import decode_access_token

    authorized = False

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        payload = decode_access_token(token)
        if payload and _is_admin_jwt(payload):
            authorized = True
            log.info("trigger-daily-newsletter: authorized via Bearer JWT for %s", payload.get("email"))

    if not authorized and pin:
        admin_pin = os.getenv("ADMIN_PIN", "")
        if admin_pin and secrets.compare_digest(pin.strip(), admin_pin.strip()):
            authorized = True
            log.info("trigger-daily-newsletter: authorized via ADMIN_PIN")
        else:
            log.warning(
                "trigger-daily-newsletter: rejected bad PIN from %s",
                request.client.host if request.client else "unknown",
            )

    if not authorized:
        return JSONResponse(
            {"status": "error", "detail": "Unauthorized — provide a valid Bearer token or ADMIN_PIN."},
            status_code=403,
        )

    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, send_daily_newsletter)
    log.info("Daily newsletter manually triggered by admin: %s", result)
    return JSONResponse({"status": "triggered", "result": str(result)})


# ---------------------------------------------------------------------------
# Admin: props pipeline diagnostic endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/test-props")
async def admin_test_props(
    request: Request,
    sport: str = "basketball_nba",
):
    """
    Admin-only diagnostic endpoint.
    Runs a live props fetch for one sport and returns raw diagnostic data as JSON.
    Useful for debugging why props are not populating on the dashboard.

    Query params:
      sport — one of basketball_nba, baseball_mlb, icehockey_nhl (default: basketball_nba)
    """
    if not _is_admin(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=403)

    from scripts.odds_fetcher import (
        get_props_df, PROP_SPORTS, PROPS_BOOKMAKERS,
        get_quota_state, PROP_MARKETS_BY_SPORT,
    )
    from models.ev_calculator import find_positive_ev_props, EV_THRESHOLD_PCT

    if sport not in PROP_SPORTS:
        return JSONResponse(
            {"error": f"Invalid sport. Must be one of: {PROP_SPORTS}"},
            status_code=400,
        )

    result: dict = {
        "sport": sport,
        "bookmakers_used": PROPS_BOOKMAKERS,
        "markets_used": PROP_MARKETS_BY_SPORT.get(sport, []),
        "ev_threshold_pct": EV_THRESHOLD_PCT,
        "quota": None,
        "props_raw_rows": 0,
        "unique_players": [],
        "unique_markets": [],
        "unique_books": [],
        "groups_total": 0,
        "ev_rows_before_threshold": 0,
        "positive_ev_rows": 0,
        "sample_rows": [],
        "error": None,
    }

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        props_df = await loop.run_in_executor(None, lambda: get_props_df(sport_keys=[sport]))

        result["props_raw_rows"] = len(props_df)
        result["quota"] = get_quota_state()  # capture post-fetch quota state

        if props_df.empty:
            result["error"] = "get_props_df returned empty DataFrame — no games in 30h window, quota exhausted, or all events returned 422/None"
            return JSONResponse(result)

        result["unique_players"] = sorted(props_df["player"].dropna().unique().tolist())[:30]
        result["unique_markets"] = sorted(props_df["prop_market"].dropna().unique().tolist())
        result["unique_books"]   = sorted(props_df["bookmaker"].dropna().unique().tolist())
        result["unique_games"]   = sorted(
            (props_df["away_team"].fillna("?") + " @ " + props_df["home_team"].fillna("?")).unique().tolist()
        )
        result["groups_total"] = props_df.groupby(
            ["game_id", "prop_market", "player", "point"], dropna=False
        ).ngroups

        ev_df = find_positive_ev_props(props_df, ev_threshold=0.0)  # threshold=0 to see all rows
        result["ev_rows_before_threshold"] = len(ev_df)
        result["positive_ev_rows"] = int((ev_df["ev_pct"] > EV_THRESHOLD_PCT).sum()) if not ev_df.empty else 0

        if not ev_df.empty:
            sample = ev_df.head(10)[["game", "market", "player_name", "outcome_name",
                                      "bookmaker", "american_odds", "ev_pct", "true_prob"]].copy()
            sample["american_odds"] = sample["american_odds"].astype(int)
            sample["ev_pct"]   = sample["ev_pct"].round(2)
            sample["true_prob"] = sample["true_prob"].round(4)
            result["sample_rows"] = sample.to_dict(orient="records")

    except Exception as exc:
        result["error"] = str(exc)
        log.error("admin/test-props: %s", exc, exc_info=True)

    return JSONResponse(result)
