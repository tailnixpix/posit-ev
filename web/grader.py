"""
web/grader.py — automatic won / lost / push grading for newsletter picks.

Final scores come from The Odds API /scores endpoint (matched by the event id
stored on each pick; 2 credits per league per run, only for leagues that have
ungraded picks). Games older than the endpoint's 3-day window fall back to
ESPN's free schedule data. Props are not auto-graded (picks are game markets).
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from db.database import Pick, SessionLocal
from scripts.espn_history import team_results

log = logging.getLogger(__name__)

_GRACE = timedelta(hours=4)       # don't grade before a game could plausibly be over
_VOID_AFTER = timedelta(days=7)   # no score found after this long → void (push)


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def settle(pick, home: str, away: str, home_score: float, away_score: float) -> Optional[str]:
    """Return 'won' | 'lost' | 'push' for a pick given the final score."""
    soccer = pick.league.startswith("soccer_")
    if pick.market == "totals":
        total = home_score + away_score
        line = float(pick.point)
        if total == line:
            return "push"
        over = pick.team.lower().startswith("over")
        return "won" if (total > line) == over else "lost"

    if _norm(pick.team) == _norm(home):
        mine, theirs = home_score, away_score
    elif _norm(pick.team) == _norm(away):
        mine, theirs = away_score, home_score
    else:
        return None   # can't tell which side we bet — leave for manual review

    if pick.market == "spreads":
        diff = mine + float(pick.point) - theirs
        return "push" if diff == 0 else ("won" if diff > 0 else "lost")
    # moneyline
    if mine == theirs:
        return "lost" if soccer else "push"   # soccer 3-way: a draw loses a win bet
    return "won" if mine > theirs else "lost"


def _fetch_scores(league: str) -> dict:
    """{event_id: event} for recently completed games in a league."""
    key = os.getenv("ODDS_API_KEY")
    if not key:
        return {}
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{league}/scores/",
            params={"apiKey": key, "daysFrom": 3}, timeout=15,
        )
        if r.status_code != 200:
            log.warning("grader: scores fetch for %s -> HTTP %s", league, r.status_code)
            return {}
        try:
            from scripts.odds_fetcher import log_credit_use
            log_credit_use("grade_scores", 2)
        except Exception:
            pass
        return {e["id"]: e for e in r.json() if e.get("completed")}
    except requests.RequestException as exc:
        log.warning("grader: scores fetch for %s failed: %s", league, exc)
        return {}


def _espn_fallback(pick) -> Optional[tuple]:
    """(home, away, home_score, away_score) from ESPN for an old game, or None."""
    from web.picks_engine import _teams_of
    away, home = _teams_of(pick.game)
    if not away or not home or not pick.commence_time:
        return None
    tr = team_results(pick.league, home)
    if not tr:
        return None
    opp = team_results(pick.league, away)
    if not opp:
        return None
    day = pick.commence_time.date()
    for g in tr["games"]:
        if g["opp_id"] != opp["team_id"] or not g["home"]:
            continue
        gd = datetime.strptime(g["date"], "%Y-%m-%d").date()
        if abs((gd - day).days) <= 1:
            return home, away, g["pf"], g["pa"]
    return None


def _score_text(away, home, a, h) -> str:
    return f"{away} {a:g} – {home} {h:g}"


def grade_pending() -> dict:
    """Grade every ungraded pick whose game should be over. Returns counts."""
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    out = {"graded": 0, "pending": 0, "voided": 0, "manual": 0}
    try:
        pending = (
            db.query(Pick)
            .filter(Pick.result.is_(None), Pick.commence_time.isnot(None),
                    Pick.commence_time < now - _GRACE)
            .all()
        )
        by_league: dict = {}
        for p in pending:
            by_league.setdefault(p.league, []).append(p)

        for league, picks in by_league.items():
            scores = _fetch_scores(league)
            for p in picks:
                home = away = hs = as_ = None
                ev = scores.get(p.game_id)
                if ev:
                    sc = {s["name"]: s["score"] for s in (ev.get("scores") or []) if s.get("score") is not None}
                    home, away = ev["home_team"], ev["away_team"]
                    try:
                        hs, as_ = float(sc[home]), float(sc[away])
                    except (KeyError, TypeError, ValueError):
                        hs = as_ = None
                if hs is None and now - p.commence_time > timedelta(days=2):
                    fb = _espn_fallback(p)
                    if fb:
                        home, away, hs, as_ = fb

                if hs is None:
                    if now - p.commence_time > _VOID_AFTER:
                        p.result, p.graded_at, p.final_score = "push", now, "No result found — voided"
                        out["voided"] += 1
                    else:
                        out["pending"] += 1
                    continue

                res = settle(p, home, away, hs, as_)
                if res is None:
                    out["manual"] += 1
                    log.warning("grader: couldn't map side for pick %s (%s)", p.id, p.team)
                    continue
                p.result, p.graded_at = res, now
                p.final_score = _score_text(away, home, as_, hs)
                out["graded"] += 1
        db.commit()
        log.info("grader: %s", out)
        return out
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
