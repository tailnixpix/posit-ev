"""
web/newsletter_v2.py — the one-stop-shop daily edition.

Builds one context from Pick rows (shadow or published) and renders it two
ways: the email (newsletter_v2.html) and the public web page (picks_edition.html).
Every number shown comes from stored facts computed in code; the only prose is
deterministic templates (an LLM narration layer can be added later without
touching any number).
"""
import json
import os
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import jinja2

from db.database import DailyPick, Pick
from web.lessons import lesson_for
from web.record import bucket, settled_picks

_CT = ZoneInfo("America/Chicago")
_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)

SPORT_LABELS = {
    "icehockey_nhl": ("NHL", "🏒"), "basketball_nba": ("NBA", "🏀"), "basketball_ncaab": ("NCAAB", "🏀"),
    "baseball_mlb": ("MLB", "⚾"), "americanfootball_nfl": ("NFL", "🏈"),
    "americanfootball_ncaaf": ("NCAAF", "🏈"), "americanfootball_cfl": ("CFL", "🏈"),
}
SOCCER_LEAGUES = {
    "soccer_epl": "Premier League", "soccer_spain_la_liga": "La Liga",
    "soccer_germany_bundesliga": "Bundesliga", "soccer_usa_mls": "MLS",
    "soccer_uefa_champs_league": "Champions League", "soccer_fifa_world_cup": "World Cup",
}
BOOK_NAMES = {
    "draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM", "caesars": "Caesars",
    "williamhill_us": "Caesars", "pointsbet": "Fanatics", "betrivers": "BetRivers",
    "espnbet": "ESPN Bet", "hardrockbet": "Hard Rock Bet", "pinnacle": "Pinnacle",
    "kalshi": "Kalshi", "novig": "NoVig", "polymarket": "Polymarket", "prophetx": "ProphetX",
}
MARKET_NAMES = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}
SPORT_ORDER = ["NHL", "NBA", "NFL", "CFL", "NCAAF", "NCAAB", "MLB", "Soccer"]


def _odds(o: Optional[int]) -> str:
    if o is None:
        return "—"
    return f"+{o}" if o > 0 else str(o)


def _sport(league: str) -> tuple:
    """(bucket label, emoji, league label) — soccer is one bucket, league shown as a tag."""
    if league.startswith("soccer_"):
        return "Soccer", "⚽", SOCCER_LEAGUES.get(league, league.split("_")[-1].upper())
    label, emoji = SPORT_LABELS.get(league, (league.split("_")[-1].upper(), "🏅"))
    return label, emoji, label


def _pick_label(p) -> str:
    if p.market == "totals" and p.point is not None:
        return f"{p.team} {p.point:g}"
    if p.market == "spreads" and p.point is not None:
        return f"{p.team} {p.point:+g}"
    return p.team


def _start(p) -> str:
    if not p.commence_time:
        return ""
    t = p.commence_time.astimezone(_CT)
    return t.strftime("%-I:%M %p CT")


def _prongs(f: dict) -> list:
    m, h, k = f.get("math") or {}, f.get("history") or {}, f.get("market") or {}
    math_lines = []
    if m:
        math_lines = [
            f"Fair win chance {m['true_pct']}% vs {m['implied_pct']}% needed at {_odds(m['odds'])} "
            f"({m['edge_pts']:+.1f} pts).",
            f"{m['ev_pct']:+.1f}% EV · {m['books']} books agree · still {m['stress_ev_pct']:+.1f}% "
            f"if we're 2 pts too high.",
        ]
    market_lines = []
    if k:
        if k.get("state") == "agrees":
            market_lines = [f"The market agrees: {_odds(k['open_odds'])} at the open → {_odds(k['now_odds'])} now "
                            f"({k['move_pts']:+.1f} pts toward this side)."]
        elif k.get("state") == "stable":
            market_lines = [f"Steady since the open ({_odds(k['open_odds'])} → {_odds(k['now_odds'])}); "
                            f"no sharp move against us."]
        if k.get("rlm_note"):
            market_lines.append(f"Sharp signal: {k['rlm_note']}.")
    return [
        {"name": "Math", "ok": bool(m.get("pass")), "lines": math_lines},
        {"name": "History", "ok": bool(h.get("pass")), "lines": (h.get("lines") or [])[:3]},
        {"name": "Market", "ok": bool(k.get("pass")), "lines": market_lines},
    ]


def _blurb(p, f: dict) -> str:
    """One compact, factual line for the card list."""
    m, h, k = f.get("math") or {}, f.get("history") or {}, f.get("market") or {}
    bits = []
    if m:
        bits.append(f"{m['ev_pct']:+.1f}% EV ({m['edge_pts']:+.1f} pts over the fair price)")
    if h.get("lines"):
        bits.append(h["lines"][0].rstrip("."))
    if k.get("state") == "agrees":
        bits.append("market agrees")
    elif k.get("state") == "stable":
        bits.append("line steady")
    return " · ".join(bits) + "."


def _story(p, f: dict) -> str:
    """Plain-English summary of why the pick is worth taking, from the stored facts only."""
    m, h, k = f.get("math") or {}, f.get("history") or {}, f.get("market") or {}
    if not m:
        return ""
    lose = round((1 - float(p.true_prob or 0.5)) * 100)
    parts = [
        f"At {_odds(m['odds'])} the price treats this as a {m['implied_pct']}% shot, but the market's "
        f"fair price says {m['true_pct']}% — a {m['edge_pts']:+.1f}-point gap."
    ]
    if h.get("pass"):
        parts.append("The team's recent form and head-to-head record back it up.")
    if k.get("state") == "agrees":
        parts.append("The line has moved toward this side since the open, so the market is leaning our way.")
    elif k.get("state") == "stable":
        parts.append("The line has held steady, with no sharp money pushing against it.")
    parts.append(
        f"Over time, bets like this are expected to return about {abs(m['ev_pct']):.1f}¢ per $1 risked — "
        f"but this one still loses roughly {lose} times in 100, so keep the stake small."
    )
    return " ".join(parts)


def _risk(p) -> str:
    lose = round((1 - float(p.true_prob or 0.5)) * 100)
    base = f"Even at this edge, we expect this to lose about {lose} times in 100."
    if p.market == "totals":
        return base + " Late injury or weather news can move a total — recheck before betting."
    return base + " Recheck the line and lineups before you bet."


def _display(p) -> dict:
    f = json.loads(p.facts) if p.facts else {}
    sport, emoji, league_label = _sport(p.league)
    return {
        "id": p.id, "rank": p.rank, "is_potd": p.is_potd,
        "sport": sport, "emoji": emoji, "league_label": league_label,
        "game": p.game or "", "label": _pick_label(p),
        "market": MARKET_NAMES.get(p.market, p.market.title()),
        "odds": _odds(p.odds), "book": BOOK_NAMES.get((p.book or "").lower(), (p.book or "").title()),
        "ev": f"{p.ev_percent:+.1f}%" if p.ev_percent is not None else "",
        "start": _start(p), "prongs": _prongs(f), "blurb": _blurb(p, f),
        "story": _story(p, f),
        "risk": _risk(p),
        "result": p.result, "final_score": p.final_score,
    }


def _result_row(p) -> dict:
    sport, emoji, _ = _sport(p.league)
    from web.record import pick_units
    return {
        "emoji": emoji, "label": _pick_label(p), "game": p.game or "", "odds": _odds(p.odds),
        "result": p.result, "units": round(pick_units(p), 2), "final": getattr(p, "final_score", None) or "",
    }


def edition_context(db, edition_date: date, status: str = "published", base_url: str = "") -> dict:
    """Everything the email and the web page need for one edition."""
    rows = (
        db.query(Pick)
        .filter(Pick.edition_date == edition_date, Pick.status == status)
        .order_by(Pick.rank.asc()).all()
    )
    picks = [_display(p) for p in rows]
    potd = next((p for p in picks if p["is_potd"]), None)
    others = [p for p in picks if not p["is_potd"]]

    groups: dict = {}
    for p in picks:
        groups.setdefault(p["sport"], []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: (SPORT_ORDER.index(kv[0]) if kv[0] in SPORT_ORDER else 99, kv[0]))
    by_sport = [{"sport": k, "emoji": v[0]["emoji"], "picks": v} for k, v in ordered]

    # Yesterday's results (this edition type; legacy single pick as a fallback).
    prev = edition_date - timedelta(days=1)
    prev_rows = (
        db.query(Pick).filter(Pick.edition_date == prev, Pick.status == status,
                              Pick.result.in_(("won", "lost", "push"))).order_by(Pick.rank.asc()).all()
    )
    if not prev_rows:
        prev_rows = db.query(DailyPick).filter(DailyPick.pick_date == prev,
                                               DailyPick.result.in_(("won", "lost", "push"))).all()
    yesterday = [_result_row(p) for p in prev_rows]
    y_bucket = bucket(prev_rows) if prev_rows else None

    record = bucket(settled_picks(db, include_shadow=(status == "shadow")))
    return {
        "edition_date": edition_date,
        "date_str": f"{edition_date.strftime('%A, %B')} {edition_date.day}, {edition_date.year}",
        "picks": picks, "potd": potd, "others": others, "by_sport": by_sport,
        "count": len(picks), "sports_count": len(groups),
        "yesterday": yesterday, "yesterday_summary": y_bucket,
        "record": record,
        "lesson": lesson_for(edition_date),
        "sponsor": None,   # set to {"name","blurb","url","cta"} to render the partner slot
        "base_url": base_url.rstrip("/"),
        "picks_url": f"{base_url.rstrip('/')}/picks/{edition_date.isoformat()}",
        "status": status,
    }


def render_email(ctx: dict, unsubscribe_url: str = "#") -> str:
    return _env.get_template("newsletter_v2.html").render(unsubscribe_url=unsubscribe_url, **ctx)
