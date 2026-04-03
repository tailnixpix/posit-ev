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
from datetime import datetime, timezone
from typing import Optional

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
        from scripts.odds_fetcher import get_props_df
        from models.ev_calculator import find_positive_ev_props
        import pandas as _pd
        ev_df = run_pipeline()

        # ── Player props (NBA, MLB, NHL) ──────────────────────────────────
        try:
            props_df = get_props_df()
            if not props_df.empty:
                props_ev_df = find_positive_ev_props(props_df)
                if not props_ev_df.empty:
                    ev_df = _pd.concat([ev_df, props_ev_df], ignore_index=True)
                    log.info("Props: found %d +EV prop bets.", len(props_ev_df))
        except Exception as _props_exc:
            log.warning("Props fetch/calc failed (non-fatal): %s", _props_exc)
    except Exception as exc:
        log.error("EV cache refresh: pipeline failed: %s", exc, exc_info=True)
        _cache_status.update({"running": False, "last_error": str(exc),
                               "last_run": datetime.now(timezone.utc)})
        return 0

    db: Session = SessionLocal()
    try:
        # Full replacement — delete all rows, insert fresh batch
        deleted = db.query(EVBetCache).delete()
        log.debug("EV cache: cleared %d stale rows.", deleted)

        if ev_df.empty:
            db.commit()
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
            try:
                odds_val = int(row.get("american_odds", 0))
            except (ValueError, TypeError):
                odds_val = 0

            point_val = row.get("point")
            try:
                point_val = float(point_val) if point_val is not None else None
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

            rows.append(EVBetCache(
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
                player_name   = str(row.get("player_name", "")) or None,
                is_prop       = bool(row.get("is_prop", False)),
                created_at    = datetime.now(timezone.utc),
            ))

        db.bulk_save_objects(rows)
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

    # Hourly refresh — runs at startup then every 60 minutes
    scheduler.add_job(
        refresh_ev_cache,
        trigger=IntervalTrigger(minutes=60),
        id="ev_cache_refresh",
        name="Refresh EV bet cache (hourly)",
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
    log.info("APScheduler started — EV cache refreshes hourly + 7:59 AM CT pre-newsletter, newsletter sends at 8 AM CT.")


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

    1. Missing / invalid JWT  → redirect /login
    2. User not subscribed    → redirect /pricing
    3. Subscribed             → pass through
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
    return {
        "wins":         wins,
        "losses":       losses,
        "pushes":       pushes,
        "total_picks":  total_picks,
        "total_profit": round(total_profit, 2),
        "roi":          round(roi, 2),
        "has_data":     total_picks > 0,
    }


# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Railway / uptime-monitor health check — returns 200 while app is alive."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, db: Session = Depends(get_db)):
    settled_picks = (
        db.query(DailyPick)
        .filter(DailyPick.result.in_(["won", "lost", "push"]))
        .order_by(DailyPick.pick_date.desc())
        .all()
    )
    pick_record = _compute_pick_record(settled_picks)
    return templates.TemplateResponse(request, "index.html", {"pick_record": pick_record})


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
    bets = (
        db.query(EVBetCache)
        .order_by(EVBetCache.ev_percent.desc())
        .all()
    )

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

    # All-time pick record (settled picks only)
    settled_picks = (
        db.query(DailyPick)
        .filter(DailyPick.result.in_(["won", "lost", "push"]))
        .order_by(DailyPick.pick_date.desc())
        .all()
    )
    pick_record = _compute_pick_record(settled_picks)

    # Compute next scheduled refresh time from the scheduler
    job = scheduler.get_job("ev_cache_refresh")
    next_refresh: Optional[datetime] = job.next_run_time if job else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user":            current_user,
            "bets":            bets,
            "bet_count":       len(bets),
            "cache_status":    _cache_status,
            "next_refresh":    next_refresh,
            "show_welcome":    welcome == "1",
            "today_pick":      today_pick,
            "pick_still_live": pick_still_live,
            "pick_record":     pick_record,
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
    user_total = db.query(User).count()
    user_paid  = db.query(User).filter(User.is_subscribed.is_(True)).count()
    user_free  = user_total - user_paid

    # Build filtered query
    query = db.query(User)
    if tier == "paid":
        query = query.filter(User.is_subscribed.is_(True))
    elif tier == "free":
        query = query.filter(User.is_subscribed.is_(False))
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

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "newsletter_subs":  newsletter_subs,
            "users":            users,
            "nl_total":         len(newsletter_subs),
            "nl_active":        sum(1 for s in newsletter_subs if s.is_active),
            # Global counts (unaffected by filter/search)
            "user_total":       user_total,
            "user_paid":        user_paid,
            "user_free":        user_free,
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


@app.post("/admin/add-pick")
async def admin_add_pick(
    request:    Request,
    pick_date:  str   = Form(...),   # YYYY-MM-DD
    game:       str   = Form(""),
    team:       str   = Form(""),
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
    point_val = float(point) if point.strip() else None
    existing = db.query(DailyPick).filter(DailyPick.pick_date == pd).first()
    if existing:
        # Update existing row rather than error
        existing.game       = game or existing.game
        existing.team       = team or existing.team
        existing.market     = market or existing.market
        existing.point      = point_val if point_val is not None else existing.point
        existing.book       = book or existing.book
        existing.odds       = odds
        existing.ev_percent = ev_percent
        existing.result     = result
        db.commit()
        log.info("Admin updated pick for %s", pd)
    else:
        db.add(DailyPick(
            pick_date  = pd,
            game       = game,
            team       = team,
            market     = market,
            point      = point_val,
            book       = book,
            odds       = odds,
            ev_percent = ev_percent,
            result     = result,
            sent_at    = datetime.now(timezone.utc),
        ))
        db.commit()
        log.info("Admin added pick for %s", pd)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/pick-result")
async def admin_update_pick_result(
    request: Request,
    pick_id: int = Form(...),
    result: str = Form(...),
    db: Session = Depends(get_db),
):
    if not _is_admin(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    """Update the result of a daily pick (won / lost / push / pending)."""
    if result not in {"won", "lost", "push", "pending"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid result '{result}'")
    pick = db.query(DailyPick).filter(DailyPick.id == pick_id).first()
    if not pick:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pick not found")
    pick.result = result
    db.commit()
    log.info("Admin updated pick %d (%s — %s) result → %s", pick_id, pick.pick_date, pick.team, result)
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
    current_user: User = Depends(require_auth),
):
    """
    Manually trigger an immediate EV cache refresh.
    Protected: requires a valid JWT (any subscribed user can trigger).
    Schedules the job to run right now via APScheduler.
    """
    job = scheduler.get_job("ev_cache_refresh")
    if job:
        scheduler.modify_job("ev_cache_refresh", next_run_time=datetime.now(timezone.utc))
        return JSONResponse({"status": "refresh queued", "triggered_by": current_user.email})
    return JSONResponse(
        {"status": "error", "detail": "Scheduler job not found"},
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
