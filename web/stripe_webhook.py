"""
web/stripe_webhook.py — Stripe Checkout + webhook handler.

Routes:
    GET  /subscribe              Create Checkout Session → redirect to Stripe
    POST /stripe/webhook         Stripe event handler (signature-verified)

Checkout flow:
    1. Logged-in user clicks "Subscribe Now" on /pricing → GET /subscribe
    2. Server creates a Stripe Checkout Session (mode=subscription)
    3. User is redirected to Stripe-hosted checkout page
    4. On success → redirected to /dashboard?checkout=success
    5. Stripe fires POST /stripe/webhook with checkout.session.completed
    6. Webhook handler sets user.is_subscribed = True

Webhook events handled:
    checkout.session.completed      → is_subscribed=True, store subscription_id
    customer.subscription.updated   → sync is_subscribed with subscription status
    customer.subscription.deleted   → is_subscribed=False
    invoice.payment_failed          → logged (no immediate access revoke)

Environment variables:
    STRIPE_SECRET_KEY
    STRIPE_PRICE_ID
    STRIPE_WEBHOOK_SECRET
    BASE_URL
"""

import logging
import os
import sys
from datetime import datetime, timezone

import stripe
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db.database import SessionLocal, User            # noqa: E402
from web.auth import (                                # noqa: E402
    decode_access_token,
    get_db,
    get_token_from_request,
    require_auth,
)

load_dotenv()

log = logging.getLogger(__name__)

stripe.api_key      = os.getenv("STRIPE_SECRET_KEY", "")
_PRICE_ID           = os.getenv("STRIPE_PRICE_ID", "")
_WEBHOOK_SECRET     = os.getenv("STRIPE_WEBHOOK_SECRET", "")
_BASE_URL           = os.getenv("BASE_URL", "http://localhost:8000")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_subscribed(
    customer_id: str,
    subscription_id: str,
    subscribed: bool,
    trial_ends_at: "datetime | None" = None,
    customer_email: str = "",
) -> bool:
    """
    Find a User by stripe_customer_id and update their subscription state.

    Falls back to email lookup when the stored customer_id is stale (e.g.
    after a test-mode → live-mode switch).  When found via email, the stored
    stripe_customer_id is updated to the new live-mode value so future
    webhook events resolve correctly.

    Returns True if a matching user was found and updated.
    """
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.stripe_customer_id == customer_id)
            .first()
        )

        if not user and customer_email:
            # Fallback: match by email (handles test→live mode transition where
            # the stored customer_id is a stale test-mode ID).
            user = (
                db.query(User)
                .filter(User.email == customer_email)
                .first()
            )
            if user:
                log.info(
                    "Webhook: matched user %s by email fallback "
                    "(old customer_id=%s → new %s)",
                    user.email, user.stripe_customer_id, customer_id,
                )
                # Persist the new live-mode customer ID so future events work.
                user.stripe_customer_id = customer_id

        if not user:
            log.warning(
                "Webhook: no user found for customer_id=%s email=%s sub=%s",
                customer_id, customer_email, subscription_id,
            )
            return False

        user.is_subscribed = subscribed
        if subscribed and subscription_id:
            user.stripe_subscription_id = subscription_id
        if trial_ends_at is not None:
            user.trial_ends_at = trial_ends_at

        db.commit()
        log.info(
            "Webhook: user %s (id=%d) is_subscribed → %s  sub_id=%s  trial_ends=%s",
            user.email, user.id, subscribed, subscription_id, trial_ends_at,
        )
        return True
    except Exception as exc:
        db.rollback()
        log.error("Webhook DB update failed: %s", exc)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /subscribe  — create Checkout Session, redirect to Stripe
# ---------------------------------------------------------------------------

@router.get("/subscribe")
async def subscribe(request: Request):
    """
    Entry point for the "Subscribe Now" button on /pricing.

    - Unauthenticated users  → /register
    - Already subscribed     → /dashboard
    - Has active Stripe sub  → heal DB flag → /dashboard  (dedup guard)
    - Authenticated + free   → create Stripe Checkout Session → redirect

    Duplicate-subscription guard: before creating a new Checkout Session,
    query Stripe for any existing active/trialing subscription on the stored
    customer ID. If one exists the DB flag is healed and the user is sent
    straight to the dashboard — no second subscription is ever created.
    """
    token = get_token_from_request(request)
    if not token:
        return RedirectResponse(url="/register", status_code=303)

    payload = decode_access_token(token)
    if not payload:
        return RedirectResponse(url="/register", status_code=303)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(payload["sub"])).first()
        if not user:
            return RedirectResponse(url="/register", status_code=303)
        if user.is_subscribed:
            return RedirectResponse(url="/dashboard", status_code=303)

        email           = user.email
        user_id         = user.id
        customer_id_db  = user.stripe_customer_id   # may be None or stale
    finally:
        db.close()

    if not stripe.api_key or stripe.api_key.startswith("sk_test_placeholder"):
        log.warning("/subscribe: STRIPE_SECRET_KEY not configured.")
        return RedirectResponse(url="/pricing?error=checkout_failed", status_code=303)

    if not _PRICE_ID:
        log.error("/subscribe: STRIPE_PRICE_ID not set.")
        return RedirectResponse(url="/pricing?error=checkout_failed", status_code=303)

    # ── Dedup guard: check Stripe for an existing active/trialing sub ─────
    # This catches the case where the user completed checkout but the webhook
    # hasn't updated the DB yet, OR where a previous checkout was completed
    # and IS_SUBSCRIBED is wrongly False due to webhook ordering.
    if customer_id_db and stripe.api_key:
        try:
            subs = stripe.Subscription.list(
                customer=customer_id_db,
                status="all",
                limit=10,
            )
            active_sub = next(
                (s for s in subs.auto_paging_iter()
                 if s.status in ("active", "trialing", "past_due")),
                None,
            )
            if active_sub:
                # Heal the DB flag and redirect — no new subscription needed
                trial_end_ts = getattr(active_sub, "trial_end", None)
                trial_ends_at = (
                    datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
                    if trial_end_ts else None
                )
                _set_subscribed(
                    customer_id_db, active_sub.id,
                    subscribed=True, trial_ends_at=trial_ends_at,
                    customer_email=email,
                )
                log.info(
                    "/subscribe: found existing %s sub %s for %s — healed DB, redirecting to dashboard.",
                    active_sub.status, active_sub.id, email,
                )
                return RedirectResponse(url="/dashboard", status_code=303)
        except stripe.error.InvalidRequestError:
            log.warning("/subscribe: stale customer_id %s for %s — will use customer_email.", customer_id_db, email)
        except stripe.error.StripeError as exc:
            log.warning("/subscribe: Stripe dedup check failed (%s) — proceeding to checkout.", exc)

    try:
        session_kwargs = {
            "mode":                     "subscription",
            "line_items":               [{"price": _PRICE_ID, "quantity": 1}],
            "success_url":              f"{_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url":               f"{_BASE_URL}/pricing",
            "allow_promotion_codes":    True,
            # 7-day free trial — card is collected but not charged until day 8
            "subscription_data":        {"trial_period_days": 7},
            "payment_method_collection": "always",
        }

        # Reuse the stored Stripe customer when available — this prevents a new
        # Customer object from being created on each checkout attempt, which was
        # the root cause of duplicate subscriptions.  Fall back to customer_email
        # only when no customer ID is stored yet (first-time subscriber).
        if customer_id_db:
            session_kwargs["customer"] = customer_id_db
        else:
            session_kwargs["customer_email"] = email

        session = stripe.checkout.Session.create(**session_kwargs)
        log.info("Checkout session %s created for %s", session.id, email)
        return RedirectResponse(url=session.url, status_code=303)

    except stripe.error.StripeError as exc:
        log.error("/subscribe Stripe error: %s", exc)
        return RedirectResponse(url="/pricing?error=checkout_failed", status_code=303)


# ---------------------------------------------------------------------------
# GET /success  — post-payment landing, syncs subscription immediately
# ---------------------------------------------------------------------------

@router.get("/success")
async def checkout_success(request: Request, session_id: str = ""):
    """
    Stripe redirects here after a successful Checkout Session.

    Retrieves the session directly from Stripe (bypasses the webhook race
    condition) and immediately activates the user's subscription before
    forwarding to /dashboard?welcome=1.

    The webhook handler still fires and is idempotent — a no-op if already
    activated here.
    """
    if not session_id:
        return RedirectResponse(url="/dashboard", status_code=303)

    if not stripe.api_key:
        return RedirectResponse(url="/dashboard", status_code=303)

    try:
        session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["subscription"],
        )
        customer_id     = session.get("customer", "")
        subscription    = session.get("subscription") or {}
        subscription_id = subscription.get("id", "") if isinstance(subscription, dict) else getattr(subscription, "id", "")

        # Extract trial end timestamp from expanded subscription object
        trial_end_ts = (
            subscription.get("trial_end")
            if isinstance(subscription, dict)
            else getattr(subscription, "trial_end", None)
        )
        trial_ends_at = (
            datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
            if trial_end_ts else None
        )

        customer_email = (
            (session.get("customer_details") or {}).get("email", "")
            or session.get("customer_email", "")
        )
        _set_subscribed(
            customer_id, subscription_id,
            subscribed=True, trial_ends_at=trial_ends_at,
            customer_email=customer_email,
        )
        log.info(
            "/success: activated subscription for customer %s email=%s session %s trial_ends=%s",
            customer_id, customer_email, session_id, trial_ends_at,
        )

    except stripe.error.StripeError as exc:
        log.error("/success Stripe error: %s", exc)
        # Still forward — webhook will activate shortly if this fails

    return RedirectResponse(url="/dashboard?welcome=1", status_code=303)


# ---------------------------------------------------------------------------
# POST /stripe/webhook  — Stripe event handler
# ---------------------------------------------------------------------------

@router.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """
    Receives and verifies Stripe webhook events.

    IMPORTANT: reads raw bytes — must not use a JSON body parser so the
    signature verification works correctly.
    """
    try:
        payload = await request.body()
    except Exception as exc:
        log.error("Stripe webhook: failed to read request body: %s", exc)
        return JSONResponse({"error": "body read failed"}, status_code=400)

    sig_header = request.headers.get("stripe-signature", "")

    if not _WEBHOOK_SECRET:
        log.error("STRIPE_WEBHOOK_SECRET not configured — rejecting webhook.")
        return JSONResponse({"error": "webhook secret not configured"}, status_code=500)

    # Verify signature
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, _WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        log.warning("Stripe webhook: invalid signature.")
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    except Exception as exc:
        log.error("Stripe webhook parse error: %s", exc)
        return JSONResponse({"error": "parse error"}, status_code=400)

    # Use attribute access (.type, .data.object) which works across all
    # stripe-python SDK versions.  Dict-style access (event["type"]) was
    # silently broken in stripe-python >= 10 and throws TypeError in v15.
    try:
        event_type = event.type
        data       = event.data.object
        event_id   = event.id
    except Exception as exc:
        log.error("Stripe webhook: failed to read event fields: %s", exc, exc_info=True)
        return JSONResponse({"error": "event parse error"}, status_code=400)

    log.info("Stripe event received: %s  id=%s", event_type, event_id)

    try:
        # ── checkout.session.completed ─────────────────────────────────────
        if event_type == "checkout.session.completed":
            customer_id     = getattr(data, "customer", "") or ""
            subscription_id = getattr(data, "subscription", "") or ""
            # customer_details is a nested object; fall back to top-level email
            _cd            = getattr(data, "customer_details", None)
            customer_email = (
                (getattr(_cd, "email", "") or "")
                if _cd else ""
            ) or (getattr(data, "customer_email", "") or "")
            # Activate regardless of payment_status — trials show "no_payment_required"
            _set_subscribed(customer_id, subscription_id, subscribed=True,
                            customer_email=customer_email)

        # ── customer.subscription.updated ─────────────────────────────────
        elif event_type == "customer.subscription.updated":
            customer_id     = getattr(data, "customer", "") or ""
            subscription_id = getattr(data, "id", "") or ""
            status          = getattr(data, "status", "") or ""
            active          = status in ("active", "trialing")
            trial_end_ts    = getattr(data, "trial_end", None)
            trial_ends_at   = (
                datetime.fromtimestamp(int(trial_end_ts), tz=timezone.utc)
                if trial_end_ts else None
            )
            _set_subscribed(customer_id, subscription_id, subscribed=active,
                            trial_ends_at=trial_ends_at)
            log.info(
                "Subscription %s status=%s → is_subscribed=%s trial_ends=%s",
                subscription_id, status, active, trial_ends_at,
            )

        # ── customer.subscription.deleted ─────────────────────────────────
        elif event_type == "customer.subscription.deleted":
            customer_id     = getattr(data, "customer", "") or ""
            subscription_id = getattr(data, "id", "") or ""
            _set_subscribed(customer_id, subscription_id, subscribed=False)

        # ── invoice.payment_failed ─────────────────────────────────────────
        elif event_type == "invoice.payment_failed":
            customer_id     = getattr(data, "customer", "") or ""
            attempt         = getattr(data, "attempt_count", 0) or 0
            subscription_id = getattr(data, "subscription", "") or ""
            log.warning("Payment failed for customer %s (attempt %s).", customer_id, attempt)
            if isinstance(attempt, int) and attempt >= 3:
                _set_subscribed(customer_id, subscription_id, subscribed=False)
                log.warning(
                    "Access revoked for customer %s after %d failed payment attempts.",
                    customer_id, attempt,
                )

        else:
            log.debug("Stripe event ignored: %s", event_type)

    except Exception as exc:
        log.error("Stripe webhook: unhandled error processing %s: %s", event_type, exc,
                  exc_info=True)
        # Return 200 so Stripe doesn't keep retrying a handler crash.
        # The error is logged for investigation.
        return JSONResponse({"status": "error", "event": event_type, "detail": str(exc)})

    # Always return 200 so Stripe doesn't retry
    return JSONResponse({"status": "ok", "event": event_type})
