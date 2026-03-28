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

def _set_subscribed(customer_id: str, subscription_id: str, subscribed: bool) -> bool:
    """
    Find a User by stripe_customer_id and update their subscription state.

    Also stores subscription_id when subscribing.
    Returns True if a matching user was found and updated.
    """
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.stripe_customer_id == customer_id)
            .first()
        )
        if not user:
            log.warning(
                "Webhook: no user found for customer_id=%s sub=%s",
                customer_id, subscription_id,
            )
            return False

        user.is_subscribed = subscribed
        if subscribed and subscription_id:
            user.stripe_subscription_id = subscription_id

        db.commit()
        log.info(
            "Webhook: user %s (id=%d) is_subscribed → %s  sub_id=%s",
            user.email, user.id, subscribed, subscription_id,
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
    - Authenticated + free   → create Stripe Checkout Session → redirect
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

        customer_id = user.stripe_customer_id
        email       = user.email
    finally:
        db.close()

    if not stripe.api_key or stripe.api_key.startswith("sk_test_placeholder"):
        log.warning("/subscribe: STRIPE_SECRET_KEY not configured.")
        return RedirectResponse(url="/pricing", status_code=303)

    if not _PRICE_ID:
        log.error("/subscribe: STRIPE_PRICE_ID not set.")
        return RedirectResponse(url="/pricing", status_code=303)

    try:
        session_kwargs = {
            "mode":                "subscription",
            "line_items":          [{"price": _PRICE_ID, "quantity": 1}],
            "success_url":         f"{_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url":          f"{_BASE_URL}/pricing",
            "allow_promotion_codes": True,
        }

        if customer_id:
            session_kwargs["customer"] = customer_id
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

        _set_subscribed(customer_id, subscription_id, subscribed=True)
        log.info("/success: activated subscription for customer %s session %s", customer_id, session_id)

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
    payload    = await request.body()
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

    event_type = event["type"]
    data       = event["data"]["object"]
    log.info("Stripe event received: %s  id=%s", event_type, event["id"])

    # ── checkout.session.completed ─────────────────────────────────────────
    if event_type == "checkout.session.completed":
        customer_id     = data.get("customer", "")
        subscription_id = data.get("subscription", "")
        payment_status  = data.get("payment_status", "")

        if payment_status == "paid":
            _set_subscribed(customer_id, subscription_id, subscribed=True)
        else:
            # subscription mode: payment_status may be "no_payment_required"
            # for trials, or the invoice is still pending — activate anyway
            # since Stripe only fires this event on successful checkout.
            _set_subscribed(customer_id, subscription_id, subscribed=True)

    # ── customer.subscription.updated ─────────────────────────────────────
    elif event_type == "customer.subscription.updated":
        customer_id     = data.get("customer", "")
        subscription_id = data.get("id", "")
        status          = data.get("status", "")
        # Active statuses that should retain dashboard access
        active = status in ("active", "trialing")
        _set_subscribed(customer_id, subscription_id, subscribed=active)
        log.info("Subscription %s status=%s → is_subscribed=%s", subscription_id, status, active)

    # ── customer.subscription.deleted ─────────────────────────────────────
    elif event_type == "customer.subscription.deleted":
        customer_id     = data.get("customer", "")
        subscription_id = data.get("id", "")
        _set_subscribed(customer_id, subscription_id, subscribed=False)

    # ── invoice.payment_failed ─────────────────────────────────────────────
    elif event_type == "invoice.payment_failed":
        customer_id     = data.get("customer", "")
        attempt         = data.get("attempt_count", 0)
        subscription_id = data.get("subscription", "")
        log.warning(
            "Payment failed for customer %s (attempt %s).",
            customer_id, attempt,
        )
        # Revoke access after 3 consecutive failures — by this point Stripe
        # has retried over several days with no success.
        if isinstance(attempt, int) and attempt >= 3:
            _set_subscribed(customer_id, subscription_id, subscribed=False)
            log.warning(
                "Access revoked for customer %s after %d failed payment attempts.",
                customer_id, attempt,
            )

    else:
        log.debug("Stripe event ignored: %s", event_type)

    # Always return 200 so Stripe doesn't retry
    return JSONResponse({"status": "ok", "event": event_type})
