"""
web/newsletter.py — Posit+EV email sender via Resend + newsletter subscription router.

FastAPI router (prefix-free, included in web/main.py):
    POST /newsletter/subscribe      Add email to NewsletterSubscriber; send welcome
    GET  /newsletter/unsubscribe    Decode JWT token → mark subscriber inactive

Scheduled function (called by APScheduler at 8 AM CT daily):
    send_daily_newsletter()         Pull top EVBetCache bet → Anthropic synopsis → send

Legacy helpers (kept for registered-user welcome / bulk sends):
    send_welcome_email(to_email)
    send_daily_picks_email(to_email, bets, date_str)
    send_newsletter(emails, bets, date_str)

Environment variables:
    RESEND_API_KEY
    BASE_URL            — constructs From address domain + unsubscribe links
    JWT_SECRET          — signs unsubscribe tokens (same secret as auth)
    ANTHROPIC_API_KEY   — claude-sonnet-4-20250514 synopsis generation
"""

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import resend
from dotenv import load_dotenv
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jose import JWTError, jwt

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import LOCAL_TZ                                   # noqa: E402
from db.database import EVBetCache, NewsletterSubscriber, SessionLocal  # noqa: E402

load_dotenv()

log = logging.getLogger(__name__)

resend.api_key = os.getenv("RESEND_API_KEY", "")

_base_url    = os.getenv("BASE_URL", "https://yourdomain.com")
_domain      = _base_url.replace("https://", "").replace("http://", "").rstrip("/")
# Strip www. prefix so From address uses root domain (matches Resend verified domain)
_email_domain = _domain[4:] if _domain.startswith("www.") else _domain
FROM_ADDRESS = f"Posit+EV <noreply@{_email_domain}>"

# JWT config (shares secret with auth.py; no new column needed)
_JWT_SECRET    = os.getenv("JWT_SECRET", "change-me")
_JWT_ALGORITHM = "HS256"

# Jinja2 environment for email templates
_WEB_DIR        = os.path.dirname(os.path.abspath(__file__))
_email_env      = Environment(
    loader        = FileSystemLoader(os.path.join(_WEB_DIR, "templates")),
    autoescape    = select_autoescape(["html"]),
    trim_blocks   = True,
    lstrip_blocks = True,
)

# ---------------------------------------------------------------------------
# Inline SVG logo
# ---------------------------------------------------------------------------

_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 120" '
    'width="320" height="120" role="img" aria-label="Posit+EV logo">'
    "<title>Posit+EV</title>"
    '<rect x="1" y="1" width="318" height="118" rx="14" ry="14" '
    'fill="#EEEDFE" stroke="#AFA9EC" stroke-width="1.5"/>'
    '<g transform="translate(18, 23)">'
    '<path d="M 40.75,19.75 C 54.5,19.75 69,31.5 78,55 L 40.75,55 Z" '
    'fill="#534AB7" fill-opacity="0.08"/>'
    '<path d="M 8,55 C 20,8 60,8 78,55" fill="none" '
    'stroke="#534AB7" stroke-width="3.5" stroke-linecap="round"/>'
    '<circle cx="40.75" cy="19.75" r="4.5" fill="#534AB7"/>'
    "</g>"
    '<text x="112" y="65" '
    'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" '
    'font-size="36" font-weight="500">'
    '<tspan fill="#26215C">Posit</tspan>'
    '<tspan fill="#534AB7">+</tspan>'
    '<tspan fill="#26215C">EV</tspan>'
    "</text>"
    '<text x="113" y="83" '
    'font-family="-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif" '
    'font-size="12" fill="#7F77DD">Find the edge. Beat the market.</text>'
    "</svg>"
)

# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

def _wrap_email(body_html: str, unsubscribe_url: str = "#") -> str:
    """Wrap *body_html* in a full branded HTML email document."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Posit+EV</title>
  <style>
    body  {{ margin: 0; padding: 0; background: #F5F4FE;
             font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
    .wrap {{ max-width: 600px; margin: 0 auto; padding: 32px 16px; }}
    .card {{ background: #ffffff; border-radius: 12px; padding: 40px 36px;
             border: 1px solid #E0DEFC; }}
    .logo-header {{ text-align: center; margin-bottom: 28px;
                    padding-bottom: 24px; border-bottom: 1px solid #EEEDFE; }}
    h2   {{ color: #26215C; font-size: 20px; margin: 0 0 12px; font-weight: 600; }}
    p    {{ color: #555270; line-height: 1.65; margin: 0 0 16px; font-size: 15px; }}
    .bet-card  {{ background: #EEEDFE; border-radius: 8px;
                  padding: 16px 20px; margin: 12px 0; }}
    .bet-ev    {{ font-size: 24px; font-weight: 700; color: #534AB7; }}
    .bet-label {{ font-size: 12px; color: #7F77DD; margin-top: 2px;
                  text-transform: uppercase; letter-spacing: 0.5px; }}
    .bet-meta  {{ font-size: 14px; color: #26215C; margin-top: 10px; line-height: 1.5; }}
    .bet-odds  {{ display: inline-block; background: #534AB7; color: #fff;
                  border-radius: 4px; padding: 2px 8px; font-size: 13px;
                  font-weight: 600; margin-left: 6px; }}
    .cta-btn   {{ display: inline-block; background: #534AB7; color: #ffffff;
                  text-decoration: none; padding: 13px 32px; border-radius: 8px;
                  font-weight: 600; font-size: 15px; margin: 20px 0 4px; }}
    .divider   {{ border: none; border-top: 1px solid #EEEDFE; margin: 24px 0; }}
    .footer    {{ text-align: center; font-size: 11px; color: #AFA9EC;
                  margin-top: 20px; line-height: 1.8; }}
    .footer a  {{ color: #AFA9EC; }}
    .synopsis  {{ background: #F7F6FE; border-left: 3px solid #534AB7;
                  border-radius: 0 8px 8px 0; padding: 14px 18px; margin: 16px 0;
                  font-size: 14px; color: #3D3860; line-height: 1.7; }}
    .tag-success {{ color: #0F6E56; font-weight: 600; }}
    .tag-accent  {{ color: #FAC775;  font-weight: 600; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="logo-header">
        {_LOGO_SVG}
      </div>
      {body_html}
    </div>
    <p class="footer">
      Posit+EV &mdash; Find the edge. Beat the market.<br>
      You&rsquo;re receiving this because you subscribed to free daily picks.<br>
      <a href="{unsubscribe_url}">Unsubscribe</a>
    </p>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Bet card formatter
# ---------------------------------------------------------------------------

def _bet_card_html(bet) -> str:
    """
    Render a single +EV bet as a branded HTML card block.

    Accepts either a dict (pipeline output) or an EVBetCache ORM object.
    """
    if isinstance(bet, dict):
        ev_pct    = bet.get("ev_pct", bet.get("effective_ev_pct", 0))
        team      = bet.get("outcome_name", bet.get("team", "—"))
        game      = bet.get("game", "")
        market    = bet.get("market", "").upper()
        book      = bet.get("bookmaker", bet.get("book", ""))
        odds      = bet.get("american_odds", bet.get("odds", ""))
        true_prob = bet.get("true_prob", 0)
    else:
        # EVBetCache ORM object
        ev_pct    = getattr(bet, "ev_percent", 0)
        team      = getattr(bet, "team", "—")
        game      = ""                             # not stored in cache
        market    = getattr(bet, "market", "").upper()
        book      = getattr(bet, "book", "")
        odds      = getattr(bet, "odds", "")
        true_prob = getattr(bet, "true_prob", 0)

    odds_str  = f"+{odds}" if isinstance(odds, (int, float)) and odds > 0 else str(odds)
    ev_color  = "#534AB7" if ev_pct > 8 else ("#0F6E56" if ev_pct > 5 else "#FAC775")

    return f"""
    <div class="bet-card">
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div class="bet-ev" style="color:{ev_color};">{ev_pct:.1f}% EV</div>
          <div class="bet-label">{market} &bull; {book}</div>
        </div>
        <span class="bet-odds">{odds_str}</span>
      </div>
      <div class="bet-meta">
        <strong>{team}</strong>
        {f'<br><span style="color:#7F77DD;">{game}</span>' if game else ""}
        <br>True prob: {true_prob:.1%}
      </div>
    </div>"""


# ---------------------------------------------------------------------------
# Send helper
# ---------------------------------------------------------------------------

def _send(to: str, subject: str, html: str) -> bool:
    """Send a single email via Resend. Returns True on success."""
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key or api_key in ("re_xxx", ""):
        log.warning("RESEND_API_KEY not configured — email to %s not sent.", to)
        return False
    try:
        resend.Emails.send({
            "from":    FROM_ADDRESS,
            "to":      [to],
            "subject": subject,
            "html":    html,
        })
        log.info("Email sent to %s: %r", to, subject)
        return True
    except Exception as exc:
        log.error("Resend failed for %s: %s", to, exc)
        return False


# ---------------------------------------------------------------------------
# Unsubscribe token helpers
# ---------------------------------------------------------------------------

def _make_unsub_token(email: str) -> str:
    """Create a signed JWT encoding an unsubscribe action for *email*."""
    return jwt.encode(
        {"sub": email, "action": "unsub"},
        _JWT_SECRET,
        algorithm=_JWT_ALGORITHM,
    )


def _decode_unsub_token(token: str) -> Optional[str]:
    """
    Decode an unsubscribe token.

    Returns the email string on success, or None if the token is invalid /
    not an unsubscribe token.
    """
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        if payload.get("action") == "unsub":
            return payload.get("sub")
        return None
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# EVBetCache query
# ---------------------------------------------------------------------------

def get_top_ev_bet() -> Optional[EVBetCache]:
    """Return the single highest-EV bet from EVBetCache, or None if empty."""
    db = SessionLocal()
    try:
        return (
            db.query(EVBetCache)
            .order_by(EVBetCache.ev_percent.desc())
            .first()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Anthropic synopsis
# ---------------------------------------------------------------------------

def _generate_synopsis(bet: EVBetCache) -> str:
    """
    Call Claude to produce a 4-5 sentence plain-English synopsis explaining:
    - The matchup / teams context
    - Why the book may have mispriced this line
    - Why the EV edge exists

    Falls back to a generic note if the API call fails or the key is missing.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("your_"):
        log.warning("ANTHROPIC_API_KEY not configured — skipping synopsis.")
        return (
            "Our model identified an edge in the current market pricing. "
            "The true probability implied by sharp-market consensus differs "
            "meaningfully from this book's line, creating a positive expected "
            "value opportunity worth tracking today."
        )

    try:
        import anthropic  # lazy import

        odds      = getattr(bet, "odds", 0)
        odds_str  = f"+{odds}" if isinstance(odds, int) and odds > 0 else str(odds)
        ev_pct    = getattr(bet, "ev_percent", 0)
        true_prob = getattr(bet, "true_prob", 0)
        team      = getattr(bet, "team", "")
        market    = getattr(bet, "market", "")
        league    = getattr(bet, "league", "")
        book      = getattr(bet, "book", "")

        prompt = (
            f"You are a sports betting analyst writing for an email newsletter. "
            f"Write exactly 4-5 sentences (plain English, no markdown, no bullet points) "
            f"explaining today's top +EV pick to a casual bettor.\n\n"
            f"Pick details:\n"
            f"  Team / outcome: {team}\n"
            f"  League: {league}\n"
            f"  Market: {market}\n"
            f"  Book: {book}\n"
            f"  American odds: {odds_str}\n"
            f"  True probability (model): {true_prob:.1%}\n"
            f"  Expected value edge: +{ev_pct:.1f}%\n\n"
            f"Cover: (1) brief context about this game or matchup, "
            f"(2) why the book may be mispricing this line, "
            f"(3) why this edge is worth acting on today. "
            f"Keep it concise, confident, and free of jargon."
        )

        client   = anthropic.Anthropic(api_key=api_key)
        message  = client.messages.create(
            model      = "claude-sonnet-4-20250514",
            max_tokens = 300,
            messages   = [{"role": "user", "content": prompt}],
        )
        synopsis = message.content[0].text.strip()
        log.info("Anthropic synopsis generated (%d chars).", len(synopsis))
        return synopsis

    except Exception as exc:
        log.error("Anthropic synopsis failed: %s", exc)
        return (
            "Our model identified a meaningful edge in today's market pricing. "
            "The consensus sharp-money probability on this outcome differs from "
            "the posted line, creating a positive expected value opportunity. "
            "Act before the market corrects."
        )


# ---------------------------------------------------------------------------
# Daily email builder
# ---------------------------------------------------------------------------

def _build_daily_email(
    bet: EVBetCache,
    synopsis: str,
    date_str: str,
    to_email: str,
) -> str:
    """
    Render newsletter_template.html for a single recipient.

    Falls back to the legacy _wrap_email() builder if the template file is
    missing (so existing tests / CLI runs are never broken).
    """
    unsub_token = _make_unsub_token(to_email)
    unsub_url   = f"{_base_url}/newsletter/unsubscribe?token={unsub_token}"

    # --- Normalise bet fields ------------------------------------------------
    odds_raw  = getattr(bet, "odds", 0)
    odds_str  = f"+{odds_raw}" if isinstance(odds_raw, int) and odds_raw > 0 else str(odds_raw)
    market_raw = getattr(bet, "market", "")
    # Pretty-print common market keys
    _market_map = {
        "h2h":     "Moneyline",
        "spreads": "Spread",
        "totals":  "Total (Over/Under)",
        "outrights": "Futures",
    }
    market_display = _market_map.get(market_raw.lower(), market_raw.title())

    league_raw = getattr(bet, "league", "")
    # Strip trailing sport suffix if present (e.g. "americanfootball_nfl" → "NFL")
    if "_" in league_raw:
        league_display = league_raw.split("_")[-1].upper()
    else:
        league_display = league_raw.upper()

    ctx = {
        "date_str":        date_str,
        "league":          league_display,
        "team":            getattr(bet, "team", "—"),
        "market":          market_display,
        "book":            getattr(bet, "book", "—"),
        "odds":            odds_str,
        "synopsis":        synopsis,
        "base_url":        _base_url,
        "unsubscribe_url": unsub_url,
    }

    try:
        tmpl = _email_env.get_template("newsletter_template.html")
        return tmpl.render(**ctx)
    except Exception as exc:
        log.warning("newsletter_template.html failed (%s) — using fallback.", exc)
        # Inline fallback so sends never fail due to a broken template
        card_html = _bet_card_html(bet)
        body = f"""
        <h2>Today&rsquo;s Free Pick &mdash; {date_str}</h2>
        <p>Here&rsquo;s the highest +EV opportunity from today&rsquo;s scan:</p>
        {card_html}
        <div class="synopsis">{synopsis}</div>
        <a class="cta-btn" href="{_base_url}/pricing">See All of Today&rsquo;s +EV Bets &rarr;</a>
        <hr class="divider" />
        <p style="font-size:13px; color:#8E8BAA;">
          EV Pro members get the full daily scan, real-time Telegram alerts,
          and Kelly-sized recommendations across all leagues.
        </p>"""
        return _wrap_email(body, unsubscribe_url=unsub_url)


# ---------------------------------------------------------------------------
# Daily newsletter orchestrator  (called by APScheduler at 8 AM CT)
# ---------------------------------------------------------------------------

def send_daily_newsletter() -> dict:
    """
    Orchestrate the daily newsletter send.

    Steps:
    1. Pull top EV bet from EVBetCache.
    2. Generate an AI synopsis via Anthropic.
    3. Fetch all active NewsletterSubscriber emails.
    4. Send individual emails (per-recipient unsubscribe token).
    5. Return summary dict {"sent": int, "failed": int, "total": int}.
    """
    date_str = datetime.now(LOCAL_TZ).strftime("%B %-d, %Y")
    subject  = f"Posit+EV | Your Free Pick \u2014 {date_str}"

    # 1. Top bet
    bet = get_top_ev_bet()
    if not bet:
        log.warning("send_daily_newsletter: no bets in EVBetCache — skipping send.")
        return {"sent": 0, "failed": 0, "total": 0}

    # 2. AI synopsis
    synopsis = _generate_synopsis(bet)

    # 3. Active subscribers
    db = SessionLocal()
    try:
        subscribers = (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.is_active.is_(True))
            .all()
        )
        emails = [s.email for s in subscribers]
    finally:
        db.close()

    if not emails:
        log.info("send_daily_newsletter: no active subscribers.")
        return {"sent": 0, "failed": 0, "total": 0}

    # 4. Send
    sent = failed = 0
    for email in emails:
        html = _build_daily_email(bet, synopsis, date_str, email)
        ok   = _send(email, subject, html)
        if ok:
            sent += 1
        else:
            failed += 1

    log.info(
        "send_daily_newsletter: %d sent, %d failed (total %d subscribers).",
        sent, failed, len(emails),
    )
    return {"sent": sent, "failed": failed, "total": len(emails)}


# ---------------------------------------------------------------------------
# Newsletter welcome (separate from registered-user welcome)
# ---------------------------------------------------------------------------

def send_newsletter_welcome(to_email: str) -> bool:
    """
    Send a welcome email to a new newsletter subscriber (free tier).

    Distinct from send_welcome_email() which targets registered accounts.
    Subject: "Welcome to Posit+EV Daily Picks"
    """
    unsub_token = _make_unsub_token(to_email)
    unsub_url   = f"{_base_url}/newsletter/unsubscribe?token={unsub_token}"

    body = f"""
    <h2>You&rsquo;re subscribed to Posit+EV Daily Picks!</h2>
    <p>
      Starting tomorrow at 8&nbsp;AM&nbsp;CT, we&rsquo;ll send you our
      single highest +EV bet of the day &mdash; complete with an expert
      breakdown of why it has edge.
    </p>
    <p class="tag-success">&#10003; Free subscription confirmed.</p>
    <p>
      Want the full dashboard? EV Pro unlocks every pick, real-time
      Telegram alerts, and half-Kelly sizing across all leagues and markets.
    </p>
    <a class="cta-btn" href="{_base_url}/pricing">View EV Pro &rarr;</a>
    <hr class="divider" />
    <p style="font-size:13px; color:#8E8BAA;">
      Didn&rsquo;t subscribe?
      <a href="{unsub_url}" style="color:#AFA9EC;">Unsubscribe here</a>.
    </p>"""

    html = _wrap_email(body, unsubscribe_url=unsub_url)
    return _send(to_email, "Welcome to Posit+EV Daily Picks", html)


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter()


@router.post("/newsletter/subscribe")
async def newsletter_subscribe(
    request: Request,
    email: str = Form(...),
):
    """
    Add an email to the newsletter subscriber list and send a welcome email.

    Handles both JSON and form POST (pricing page AJAX uses form encoding).
    Idempotent: re-activates inactive subscribers rather than creating duplicates.
    """
    email = email.strip().lower()

    # Basic format check
    if "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse(
            {"status": "error", "detail": "Invalid email address."},
            status_code=400,
        )

    db = SessionLocal()
    try:
        existing = (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.email == email)
            .first()
        )

        if existing:
            if existing.is_active:
                return JSONResponse(
                    {"status": "already_subscribed",
                     "message": "You're already subscribed to daily picks."},
                    status_code=200,
                )
            # Re-activate lapsed subscriber
            existing.is_active = True
            db.commit()
            log.info("Newsletter: re-activated subscriber %s", email)
        else:
            subscriber = NewsletterSubscriber(
                email       = email,
                subscribed_at = datetime.now(timezone.utc),
                is_active   = True,
            )
            db.add(subscriber)
            db.commit()
            log.info("Newsletter: new subscriber %s", email)

    except Exception as exc:
        db.rollback()
        log.error("Newsletter subscribe DB error for %s: %s", email, exc)
        return JSONResponse(
            {"status": "error", "detail": "Database error. Please try again."},
            status_code=500,
        )
    finally:
        db.close()

    # Send welcome (non-blocking: errors are logged but don't fail the response)
    try:
        send_newsletter_welcome(email)
    except Exception as exc:
        log.error("Newsletter welcome email failed for %s: %s", email, exc)

    return JSONResponse(
        {"status": "subscribed",
         "message": "You're subscribed! Check your inbox for a welcome email."},
        status_code=200,
    )


@router.get("/newsletter/unsubscribe", response_class=HTMLResponse)
async def newsletter_unsubscribe(token: str = ""):
    """
    One-click unsubscribe via signed JWT token embedded in every email footer.

    GET /newsletter/unsubscribe?token=<jwt>
    """
    if not token:
        return _unsub_page(
            success=False,
            message="Missing unsubscribe token. Please use the link from your email.",
        )

    email = _decode_unsub_token(token)
    if not email:
        return _unsub_page(
            success=False,
            message="This unsubscribe link is invalid or has expired.",
        )

    db = SessionLocal()
    try:
        subscriber = (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.email == email)
            .first()
        )
        if subscriber and subscriber.is_active:
            subscriber.is_active = False
            db.commit()
            log.info("Newsletter: unsubscribed %s", email)
        elif not subscriber:
            log.warning("Newsletter unsubscribe: no record found for %s", email)
    except Exception as exc:
        db.rollback()
        log.error("Newsletter unsubscribe DB error for %s: %s", email, exc)
        return _unsub_page(
            success=False,
            message="Something went wrong. Please try again later.",
        )
    finally:
        db.close()

    return _unsub_page(
        success=True,
        message=f"{email} has been unsubscribed from Posit+EV daily picks.",
    )


def _unsub_page(success: bool, message: str) -> str:
    """Minimal branded HTML confirmation page for unsubscribe results."""
    icon    = "✓" if success else "✕"
    color   = "#0F6E56" if success else "#C94040"
    heading = "Unsubscribed" if success else "Link Error"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Posit+EV — {heading}</title>
  <style>
    body {{ margin: 0; padding: 0; background: #F5F4FE;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
    .wrap {{ max-width: 480px; margin: 80px auto; padding: 0 16px; text-align: center; }}
    .icon {{ font-size: 48px; color: {color}; margin-bottom: 12px; }}
    h1   {{ color: #26215C; font-size: 24px; margin: 0 0 12px; }}
    p    {{ color: #555270; font-size: 15px; line-height: 1.6; }}
    a    {{ color: #534AB7; font-weight: 600; text-decoration: none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="icon">{icon}</div>
    <h1>{heading}</h1>
    <p>{message}</p>
    <p style="margin-top:24px;">
      <a href="{_base_url}/">← Back to Posit+EV</a>
    </p>
  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Legacy public API (registered-user welcome + bulk pick sends)
# ---------------------------------------------------------------------------

def send_welcome_email(to_email: str) -> bool:
    """
    Send the welcome email to a newly registered (paid-account) user.

    Subject: "Welcome to Posit+EV"
    """
    body = f"""
    <h2>Welcome to Posit+EV!</h2>
    <p>
      You&rsquo;re in. We scan odds from DraftKings, FanDuel, BetMGM, and more
      to find bets where the books have it wrong &mdash; so you can bet with
      the edge, not against it.
    </p>
    <p class="tag-success">&#10003; Your account is ready.</p>
    <p>
      Subscribe to unlock the full dashboard: real-time +EV picks,
      sport-specific adjustments, and a Telegram bot that alerts you the moment
      a high-value opportunity appears.
    </p>
    <a class="cta-btn" href="{_base_url}/pricing">View Pricing &rarr;</a>
    <hr class="divider" />
    <p style="font-size:13px; color:#8E8BAA;">
      If you didn&rsquo;t create this account, you can safely ignore this email.
    </p>"""

    html = _wrap_email(body)
    return _send(to_email, "Welcome to Posit+EV", html)


def send_daily_picks_email(
    to_email: str,
    bets: list,
    date_str: Optional[str] = None,
) -> bool:
    """
    Send the daily free picks email (bulk/legacy version — accepts list of dicts).

    Subject: "Posit+EV | Your Free Pick — <date>"
    """
    if not date_str:
        date_str = datetime.now(LOCAL_TZ).strftime("%B %-d, %Y")

    subject = f"Posit+EV | Your Free Pick \u2014 {date_str}"

    if not bets:
        body = f"""
        <h2>Today&rsquo;s picks &mdash; {date_str}</h2>
        <p>No +EV bets cleared our threshold today. Check back tomorrow &mdash;
           or subscribe to monitor live odds all day.</p>
        <a class="cta-btn" href="{_base_url}/dashboard">Open Dashboard &rarr;</a>"""
        return _send(to_email, subject, _wrap_email(body))

    featured   = bets[0]
    extras_html = "".join(_bet_card_html(b) for b in bets[1:3])
    extras_section = ""
    if extras_html:
        extras_section = f"""
        <hr class="divider" />
        <h2 style="font-size:16px; color:#7F77DD;">More picks today</h2>
        {extras_html}
        <p style="font-size:13px; color:#AFA9EC;">
          Subscribe for the full daily scan across all leagues and markets.
        </p>"""

    body = f"""
    <h2>Today&rsquo;s Free Pick &mdash; {date_str}</h2>
    <p>Here&rsquo;s your highest-EV opportunity from today&rsquo;s scan:</p>
    {_bet_card_html(featured)}
    <a class="cta-btn" href="{_base_url}/dashboard">See All Picks &rarr;</a>
    {extras_section}"""

    return _send(to_email, subject, _wrap_email(body))


def send_newsletter(
    emails: list,
    bets: list,
    date_str: Optional[str] = None,
) -> dict:
    """
    Send the daily picks email to a list of recipients (legacy bulk API).

    Returns {"sent": int, "failed": int, "total": int}.
    """
    sent = failed = 0
    for email in emails:
        ok = send_daily_picks_email(email, bets, date_str)
        if ok:
            sent += 1
        else:
            failed += 1

    log.info("Newsletter sent: %d/%d succeeded", sent, len(emails))
    return {"sent": sent, "failed": failed, "total": len(emails)}


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Posit+EV email sender")
    parser.add_argument("--to",        required=True,      help="Recipient email address")
    parser.add_argument("--welcome",   action="store_true", help="Send account welcome email")
    parser.add_argument("--nl-welcome",action="store_true", help="Send newsletter welcome email")
    parser.add_argument("--picks",     action="store_true", help="Send daily picks email (legacy)")
    parser.add_argument("--daily",     action="store_true", help="Run full send_daily_newsletter()")
    args = parser.parse_args()

    if args.welcome:
        print("Account welcome email sent:", send_welcome_email(args.to))

    if args.nl_welcome:
        print("Newsletter welcome email sent:", send_newsletter_welcome(args.to))

    if args.picks:
        dummy_bets = [
            {"outcome_name": "Boston Bruins", "game": "Bruins @ Maple Leafs",
             "market": "h2h", "bookmaker": "draftkings", "american_odds": 145,
             "true_prob": 0.44, "ev_pct": 9.3, "effective_ev_pct": 9.3},
            {"outcome_name": "Denver Nuggets -4.5", "game": "Lakers @ Nuggets",
             "market": "spreads", "bookmaker": "fanduel", "american_odds": -108,
             "true_prob": 0.54, "ev_pct": 4.7, "effective_ev_pct": 4.7},
        ]
        print("Picks email sent:", send_daily_picks_email(args.to, dummy_bets))

    if args.daily:
        result = send_daily_newsletter()
        print("Daily newsletter result:", result)
