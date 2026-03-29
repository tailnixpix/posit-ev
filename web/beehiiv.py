"""
web/beehiiv.py — Beehiiv API v2 client for Posit+EV.

Handles subscriber sync and newsletter post creation so that Beehiiv can
monetize the newsletter (ad network, Boosts, paid upgrades).

Functions:
    add_subscriber(email)           — add/reactivate a subscriber
    remove_subscriber(email)        — unsubscribe by email
    bulk_sync(emails)               — sync a list of emails (for migration)
    create_post(subject, body_html) — create and send a newsletter post

Environment variables required:
    BEEHIIV_API_KEY           — API key from app.beehiiv.com → Settings → API
    BEEHIIV_PUBLICATION_ID    — pub_XXXXXXXX from the same page

When either env var is missing every function is a silent no-op so the app
works normally during local development before Beehiiv is connected.
"""

import logging
import os

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.beehiiv.com/v2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    return os.getenv("BEEHIIV_API_KEY", "")


def _pub_id() -> str:
    return os.getenv("BEEHIIV_PUBLICATION_ID", "")


def _enabled() -> bool:
    return bool(_api_key() and _pub_id())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Subscriber management
# ---------------------------------------------------------------------------

def add_subscriber(email: str) -> bool:
    """
    Add or reactivate a subscriber in Beehiiv.

    Called automatically whenever someone signs up via the website form or
    registers an account. If the email already exists in Beehiiv it is
    reactivated rather than duplicated.

    Returns True on success, False on any error (errors are logged but never
    raised — subscriber sync should never block a user-facing request).
    """
    if not _enabled():
        log.debug("Beehiiv not configured — skipping add_subscriber for %s", email)
        return False

    try:
        resp = requests.post(
            f"{_BASE}/publications/{_pub_id()}/subscriptions",
            headers=_headers(),
            json={
                "email": email,
                "reactivate_existing": True,
                "send_welcome_email": False,   # Posit+EV sends its own welcome via Resend
                "utm_source": "posit_ev_website",
                "utm_medium": "organic",
            },
            timeout=10,
        )
        if resp.status_code in (200, 201):
            log.info("Beehiiv: subscriber added/reactivated — %s", email)
            return True

        log.warning(
            "Beehiiv add_subscriber failed for %s: HTTP %s — %s",
            email, resp.status_code, resp.text[:200],
        )
        return False

    except Exception as exc:
        log.error("Beehiiv add_subscriber error for %s: %s", email, exc)
        return False


def remove_subscriber(email: str) -> bool:
    """
    Unsubscribe an email address from Beehiiv.

    First resolves the subscriber record by email (list endpoint), then
    calls the DELETE endpoint. Returns True if the subscriber was removed
    or was not found (idempotent).
    """
    if not _enabled():
        log.debug("Beehiiv not configured — skipping remove_subscriber for %s", email)
        return False

    try:
        # 1. Look up subscription ID by email
        resp = requests.get(
            f"{_BASE}/publications/{_pub_id()}/subscriptions",
            headers=_headers(),
            params={"email": email, "limit": 1},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("Beehiiv lookup failed for %s: HTTP %s", email, resp.status_code)
            return False

        data = resp.json().get("data", [])
        if not data:
            log.info("Beehiiv: no subscription found for %s — nothing to remove.", email)
            return True   # not an error — subscriber simply wasn't in Beehiiv

        sub_id = data[0]["id"]

        # 2. Delete the subscription
        del_resp = requests.delete(
            f"{_BASE}/publications/{_pub_id()}/subscriptions/{sub_id}",
            headers=_headers(),
            timeout=10,
        )
        if del_resp.status_code in (200, 204):
            log.info("Beehiiv: unsubscribed %s (id=%s)", email, sub_id)
            return True

        log.warning(
            "Beehiiv remove_subscriber failed for %s: HTTP %s — %s",
            email, del_resp.status_code, del_resp.text[:200],
        )
        return False

    except Exception as exc:
        log.error("Beehiiv remove_subscriber error for %s: %s", email, exc)
        return False


def bulk_sync(emails: list[str]) -> dict:
    """
    Sync a list of email addresses to Beehiiv.

    Used by the admin one-time migration endpoint to push all existing
    NewsletterSubscribers into Beehiiv in a single call.

    Returns {"synced": int, "failed": int, "total": int}.
    """
    if not _enabled():
        log.warning("Beehiiv not configured — bulk_sync skipped.")
        return {"synced": 0, "failed": 0, "total": len(emails)}

    synced = failed = 0
    for email in emails:
        ok = add_subscriber(email)
        if ok:
            synced += 1
        else:
            failed += 1

    log.info("Beehiiv bulk_sync: %d synced, %d failed out of %d.", synced, failed, len(emails))
    return {"synced": synced, "failed": failed, "total": len(emails)}


# ---------------------------------------------------------------------------
# Newsletter post creation
# ---------------------------------------------------------------------------

def create_post(
    subject: str,
    body_html: str,
    subtitle: str = "",
    send: bool = True,
) -> dict:
    """
    Create a newsletter post in Beehiiv and optionally send it immediately.

    When send=True the post status is set to "confirmed" and Beehiiv delivers
    it to all active subscribers — replacing the per-recipient Resend loop.
    When send=False the post is saved as a draft for manual review in the
    Beehiiv dashboard before sending.

    body_html should be the inner post content only (not a full HTML document).
    Beehiiv applies its own email chrome (header, footer, unsubscribe link).

    Returns the post data dict from Beehiiv, or {} on failure.
    """
    if not _enabled():
        log.warning("Beehiiv not configured — create_post skipped.")
        return {}

    status = "confirmed" if send else "draft"

    try:
        resp = requests.post(
            f"{_BASE}/publications/{_pub_id()}/posts",
            headers=_headers(),
            json={
                "subject":          subject,
                "subtitle":         subtitle,
                "content_html":     body_html,   # Beehiiv wraps this in their email template
                "status":           status,
                "audience":         "free",      # send to free tier (all subscribers)
            },
            timeout=30,
        )

        if resp.status_code in (200, 201):
            post = resp.json().get("data", {})
            log.info(
                "Beehiiv post created: id=%s status=%s subject=%r",
                post.get("id"), status, subject,
            )
            return post

        log.error(
            "Beehiiv create_post failed: HTTP %s — %s",
            resp.status_code, resp.text[:400],
        )
        return {}

    except Exception as exc:
        log.error("Beehiiv create_post error: %s", exc)
        return {}
