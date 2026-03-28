"""
web/auth.py — Authentication primitives, dependencies, and router for Posit+EV.

Primitives (importable by main.py and other modules):
    hash_password(plain)           → bcrypt hash
    verify_password(plain, hashed) → bool
    create_access_token(id, email) → signed JWT string
    decode_access_token(token)     → payload dict | None
    get_token_from_request(req)    → cookie value | None

Dependencies:
    get_current_user()  — returns User or raises HTTP 401  (API routes)
    require_auth()      — returns User or raises RedirectException  (HTML routes)

Exception handling:
    RedirectException              — raised by require_auth to trigger a redirect
    setup_exception_handlers(app)  — register the handler on the FastAPI app instance

Router (include in main.py via app.include_router):
    POST /register  — validate → create User → create Stripe customer → set JWT cookie
    POST /login     — verify credentials → set JWT cookie → redirect /dashboard
    POST /logout    — delete JWT cookie → redirect /

Usage in main.py:
    from web.auth import (
        router as auth_router,
        get_current_user, require_auth,
        create_access_token, decode_access_token, get_token_from_request,
        RedirectException, setup_exception_handlers,
    )
    setup_exception_handlers(app)
    app.include_router(auth_router)
"""

import os
import sys
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import stripe
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db.database import SessionLocal, User, NewsletterSubscriber  # noqa: E402

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JWT_SECRET: str         = os.getenv("JWT_SECRET", "change-me-in-production")
JWT_ALGORITHM: str      = "HS256"
JWT_EXPIRE_MINUTES: int = 60 * 24 * 7   # 7 days

COOKIE_NAME          = "access_token"
MIN_PASSWORD_LENGTH  = 8

# ---------------------------------------------------------------------------
# Promo / referral codes
# ---------------------------------------------------------------------------
# Comma-separated list of valid codes in env var PROMO_CODES.
# Default includes POSI2. Add more: PROMO_CODES=POSI2,BETA50,FRIEND
def _load_promo_codes() -> set:
    raw = os.getenv("PROMO_CODES", "POSI2")
    return {c.strip().upper() for c in raw.split(",") if c.strip()}

def is_valid_promo(code: str) -> bool:
    return code.strip().upper() in _load_promo_codes()

_PLACEHOLDER_KEYS = {"sk_live_xxx", "sk_test_xxx", "", None}

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_WEB_DIR    = os.path.dirname(os.path.abspath(__file__))
_templates  = Jinja2Templates(directory=os.path.join(_WEB_DIR, "templates"))

# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the stored bcrypt *hashed* value."""
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(user_id: int, email: str) -> str:
    """Sign and return a JWT encoding *user_id* and *email*."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "email": email, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and verify a JWT. Returns the payload dict or None on failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


def get_token_from_request(request: Request) -> Optional[str]:
    """Extract the access_token cookie value from the request."""
    return request.cookies.get(COOKIE_NAME)


def _set_auth_cookie(response, token: str) -> None:
    """Attach the JWT as an HTTP-only cookie to *response* (mutates in place)."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=os.getenv("BASE_URL", "").startswith("https"),
        samesite="lax",
        max_age=JWT_EXPIRE_MINUTES * 60,
    )


# ---------------------------------------------------------------------------
# Database dependency
# ---------------------------------------------------------------------------

def get_db():
    """Yield a SQLAlchemy session; close it after the request completes."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Custom exception for HTML redirect flows
# ---------------------------------------------------------------------------

class RedirectException(Exception):
    """Raised by require_auth to trigger a browser redirect instead of a 401."""
    def __init__(self, url: str, status_code: int = status.HTTP_303_SEE_OTHER):
        self.url = url
        self.status_code = status_code


def setup_exception_handlers(app) -> None:
    """
    Register auth exception handlers on the FastAPI *app* instance.
    Call this once in web/main.py after creating the app object:

        setup_exception_handlers(app)
    """
    @app.exception_handler(RedirectException)
    async def _redirect_handler(request: Request, exc: RedirectException):
        return RedirectResponse(url=exc.url, status_code=exc.status_code)


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency for **API routes** (returns JSON).
    Raises HTTP 401 if the JWT cookie is missing, invalid, or expired.

    Usage:
        @app.get("/api/me")
        def me(user: User = Depends(get_current_user)):
            return {"email": user.email}
    """
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


def require_auth(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency for **HTML routes**.
    Raises RedirectException (→ /login) instead of 401 so the browser
    gets a proper redirect rather than a JSON error.

    Usage:
        @app.get("/dashboard")
        def dashboard(user: User = Depends(require_auth)):
            ...
    """
    token = get_token_from_request(request)
    if not token:
        raise RedirectException(url="/login")
    payload = decode_access_token(token)
    if not payload:
        raise RedirectException(url="/login")
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise RedirectException(url="/login")
    return user


# ---------------------------------------------------------------------------
# Stripe helpers
# ---------------------------------------------------------------------------

def _stripe_is_configured() -> bool:
    sk = os.getenv("STRIPE_SECRET_KEY", "")
    return bool(sk) and sk not in _PLACEHOLDER_KEYS and not sk.endswith("_xxx")


def _create_stripe_customer(email: str) -> Optional[str]:
    """
    Create a Stripe Customer object and return its ID.
    Returns None (without raising) when Stripe is not configured or the API
    call fails, so registration can still succeed without Stripe.
    """
    if not _stripe_is_configured():
        log.warning("Stripe not configured — skipping customer creation for %s", email)
        return None
    try:
        customer = stripe.Customer.create(
            email=email,
            metadata={"source": "positiev_web"},
        )
        return customer.id
    except stripe.error.StripeError as exc:
        log.error("Stripe customer creation failed for %s: %s", email, exc)
        return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/check-promo")
async def check_promo(code: str = ""):
    """AJAX endpoint — returns {valid: bool} for live promo code feedback."""
    return JSONResponse({"valid": is_valid_promo(code)})


@router.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    email: str      = Form(...),
    password: str   = Form(...),
    promo_code: str = Form(""),
    db: Session     = Depends(get_db),
):
    """
    Create a new user account.

    Steps:
      1. Validate email format and password length.
      2. Check for duplicate email.
      3. Hash password with bcrypt.
      4. Create Stripe Customer (graceful no-op if Stripe not configured).
      5. Persist User to DB.
      6. If valid promo code → grant is_subscribed = True immediately.
      7. Auto-subscribe to newsletter + send welcome email.
      8. Issue JWT cookie and redirect:
           - promo granted  → /welcome
           - no promo       → /pricing
    """
    email      = email.lower().strip()
    promo_code = promo_code.strip().upper()
    promo_ok   = is_valid_promo(promo_code)

    if not _valid_email(email):
        return _templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Please enter a valid email address."},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if len(password) < MIN_PASSWORD_LENGTH:
        return _templates.TemplateResponse(
            request,
            "register.html",
            {"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if db.query(User).filter(User.email == email).first():
        return _templates.TemplateResponse(
            request,
            "register.html",
            {"error": "An account with that email already exists.",
             "prefill_email": email},
            status_code=status.HTTP_409_CONFLICT,
        )

    if promo_code and not promo_ok:
        return _templates.TemplateResponse(
            request,
            "register.html",
            {"error": "That promo code is invalid. Leave it blank or enter a valid code.",
             "prefill_email": email},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    stripe_customer_id = _create_stripe_customer(email)

    user = User(
        email=email,
        hashed_password=hash_password(password),
        is_subscribed=promo_ok,   # grant access immediately if promo is valid
        stripe_customer_id=stripe_customer_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info(
        "Registered: %s  stripe_customer=%s  promo=%s",
        email, stripe_customer_id or "none", promo_code or "none",
    )

    # Auto-subscribe to newsletter (idempotent — skip if already subscribed)
    try:
        existing_sub = (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.email == email)
            .first()
        )
        if not existing_sub:
            db.add(NewsletterSubscriber(email=email, is_active=True))
            db.commit()
            log.info("Auto-subscribed %s to newsletter on registration.", email)
        # Send newsletter welcome email (non-blocking)
        from web.newsletter import send_newsletter_welcome  # lazy import avoids circular
        send_newsletter_welcome(email)
    except Exception as exc:
        log.error("Auto-newsletter signup failed for %s: %s", email, exc)

    token    = create_access_token(user.id, user.email)
    redirect = "/welcome" if promo_ok else "/pricing"
    response = RedirectResponse(url=redirect, status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    return response


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str    = Form(...),
    password: str = Form(...),
    db: Session   = Depends(get_db),
):
    """
    Verify credentials and issue a JWT cookie.
    On success:  redirect → /dashboard
    On failure:  re-render login.html with an error message (HTTP 401).
    """
    user = db.query(User).filter(User.email == email.lower().strip()).first()

    if not user or not verify_password(password, user.hashed_password):
        return _templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid email or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_access_token(user.id, user.email)
    response = RedirectResponse(url="/welcome", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    log.info("Login: %s", user.email)
    return response


@router.post("/logout")
async def logout():
    """
    Clear the JWT cookie and redirect to the landing page.
    Uses POST (not GET) to prevent CSRF via embedded links.
    """
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        samesite="lax",
    )
    return response
