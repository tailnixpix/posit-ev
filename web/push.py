"""
web/push.py — Web Push alerts for new high-EV picks.

Env (optional — the feature hides itself when unset):
    VAPID_PUBLIC_KEY   base64url uncompressed P-256 public key
    VAPID_PRIVATE_KEY  base64url private key
    VAPID_SUBJECT      mailto: contact (default mailto:support.positev@gmail.com)
"""
import json
import logging
import os
from datetime import datetime

from pytz import timezone as _tz

from db.database import PushSubscription, SessionLocal, User

log = logging.getLogger(__name__)

DEFAULT_MIN_EV = 5.0
_QUIET_START_HOUR_CT = 23   # no pushes 11 PM – 7 AM CT
_QUIET_END_HOUR_CT = 7


def public_key() -> str:
    return os.getenv("VAPID_PUBLIC_KEY", "")


def is_configured() -> bool:
    return bool(public_key() and os.getenv("VAPID_PRIVATE_KEY"))


def _is_quiet_hours() -> bool:
    h = datetime.now(_tz("America/Chicago")).hour
    return h >= _QUIET_START_HOUR_CT or h < _QUIET_END_HOUR_CT


def notify_new_picks(new_picks: list) -> int:
    """
    Push a summary of newly-appeared +EV picks to every subscribed device whose
    min_ev threshold is met. `new_picks` items need .ev_percent, .team, .game,
    .league. Returns the number of notifications delivered.
    """
    if not new_picks or not is_configured() or _is_quiet_hours():
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("push: pywebpush not installed — skipping alerts")
        return 0

    new_picks = sorted(new_picks, key=lambda b: b.ev_percent, reverse=True)
    vapid_claims = {"sub": os.getenv("VAPID_SUBJECT", "mailto:support.positev@gmail.com")}
    sent = 0
    db = SessionLocal()
    try:
        subs = (
            db.query(PushSubscription)
            .join(User, User.id == PushSubscription.user_id)
            .filter(User.is_subscribed == True)  # noqa: E712
            .all()
        )
        for sub in subs:
            mine = [b for b in new_picks if b.ev_percent >= (sub.min_ev or DEFAULT_MIN_EV)]
            if not mine:
                continue
            top = mine[0]
            if len(mine) == 1:
                body = f"{top.team} — {top.game or top.league}"
            else:
                body = f"{top.team} leads {len(mine)} new picks"
            payload = json.dumps({
                "title": f"New +{top.ev_percent:.1f}% EV pick",
                "body": body,
                "url": "/dashboard",
            })
            try:
                webpush(
                    subscription_info={"endpoint": sub.endpoint,
                                       "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    data=payload,
                    vapid_private_key=os.getenv("VAPID_PRIVATE_KEY"),
                    vapid_claims=dict(vapid_claims),
                    ttl=3600,
                )
                sent += 1
            except WebPushException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):
                    db.delete(sub)   # device unsubscribed / uninstalled
                else:
                    log.warning("push: send failed (%s): %s", code, exc)
            except Exception as exc:
                log.warning("push: send error: %s", exc)
        db.commit()
    finally:
        db.close()
    return sent
