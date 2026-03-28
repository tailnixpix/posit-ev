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
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
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

from db.database import EVBetCache, NewsletterSubscriber, SessionLocal, User, create_tables  # noqa: E402
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

load_dotenv()

log = logging.getLogger(__name__)

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
        ev_df = run_pipeline()
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

            rows.append(EVBetCache(
                league     = str(row.get("sport_key",      "")),
                market     = str(row.get("market",         "")),
                team       = str(row.get("outcome_name",   "")),
                game       = str(row.get("game",           "")) or None,
                point      = point_val,
                book       = str(row.get("bookmaker",      "")),
                ev_percent = float(row.get("effective_ev_pct", row.get("ev_pct", 0))),
                true_prob  = float(row.get("true_prob",    0)),
                odds       = odds_val,
                created_at = datetime.now(timezone.utc),
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
            _db.commit()
        except Exception:
            _db.rollback()

    # Schedule refresh every 30 minutes, starting immediately (next_run_time=now)
    scheduler.add_job(
        refresh_ev_cache,
        trigger=IntervalTrigger(minutes=60),
        id="ev_cache_refresh",
        name="Refresh EV bet cache",
        next_run_time=datetime.now(timezone.utc),   # run once at startup
        misfire_grace_time=120,
        replace_existing=True,
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

    scheduler.start()
    log.info("APScheduler started — EV cache refreshes every 30 min, newsletter at 8 AM CT.")


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


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    token = get_token_from_request(request)
    if token and decode_access_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    return templates.TemplateResponse(request, "pricing.html")


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

    # Compute next scheduled refresh time from the scheduler
    job = scheduler.get_job("ev_cache_refresh")
    next_refresh: Optional[datetime] = job.next_run_time if job else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user":         current_user,
            "bets":         bets,
            "bet_count":    len(bets),
            "cache_status": _cache_status,
            "next_refresh": next_refresh,
            "show_welcome": welcome == "1",
        },
    )


# ---------------------------------------------------------------------------
# Manual cache refresh (admin)
# ---------------------------------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Admin dashboard — protected by ADMIN_PASSWORD env var.
    Shows newsletter subscribers and registered users.
    """
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    provided = request.query_params.get("key", "")
    if not admin_password or provided != admin_password:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:60px;text-align:center;'>"
            "<h2>Access denied</h2><p>Append <code>?key=YOUR_ADMIN_PASSWORD</code> to the URL.</p>"
            "</body></html>",
            status_code=403,
        )

    newsletter_subs = (
        db.query(NewsletterSubscriber)
        .order_by(NewsletterSubscriber.subscribed_at.desc())
        .all()
    )
    users = db.query(User).order_by(User.id.desc()).all()

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "newsletter_subs": newsletter_subs,
            "users":           users,
            "nl_total":        len(newsletter_subs),
            "nl_active":       sum(1 for s in newsletter_subs if s.is_active),
            "user_total":      len(users),
            "user_paid":       sum(1 for u in users if u.is_subscribed),
            "now":             datetime.now(timezone.utc).strftime("%b %-d, %Y at %-I:%M %p UTC"),
            "admin_key":       provided,
        },
    )


@app.post("/admin/grant-access")
async def admin_grant_access(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    provided = request.query_params.get("key", "")
    if not admin_password or provided != admin_password:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_subscribed = True
        db.commit()
        log.info("Admin granted access to %s", user.email)
    return RedirectResponse(url=f"/admin?key={provided}", status_code=303)


@app.post("/admin/revoke-access")
async def admin_revoke_access(
    request: Request,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    provided = request.query_params.get("key", "")
    if not admin_password or provided != admin_password:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.is_subscribed = False
        db.commit()
        log.info("Admin revoked access from %s", user.email)
    return RedirectResponse(url=f"/admin?key={provided}", status_code=303)


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
