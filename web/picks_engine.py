"""
web/picks_engine.py — the three-prong newsletter pick engine.

A bet is publishable only when it passes ALL THREE prongs:

  1. MATH     — a certain +EV: EV >= MIN_EV against the no-vig market price,
                priced by >= MIN_BOOKS books, still +EV if our probability is
                off by STRESS_PTS points.
  2. HISTORY  — the pick fits the team's results: form, venue split, results
                against *today's* spread / total, and head-to-head meetings
                (free ESPN data).
  3. MARKET   — the line hasn't moved against us since it opened. If it moved
                our way the market agrees with us, and we say so.

Selection: one pick per game, up to PER_SPORT_CAP per sport (all soccer leagues combined), hard cap
TOTAL_CAP per edition. Every number the email shows is computed here in code —
nothing numeric is ever written by an LLM.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from db.database import EVBetCache, Pick, SessionLocal
from scripts.espn_history import team_results

log = logging.getLogger(__name__)

_CT = ZoneInfo("America/Chicago")

TOTAL_CAP = 10          # hard cap on picks per edition
PER_SPORT_CAP = 3       # soccer counts as ONE sport across every league we cover
MIN_EV = 3.0            # % — prong 1
MIN_BOOKS = 3           # prong 1: books pricing this exact line
MIN_TRUE_PROB = 0.40    # no longshots
STRESS_PTS = 0.02       # prong 1: EV must survive a 2-point haircut to our probability
AGREES_PTS = 0.5        # prong 3: implied prob rose >= this many points → market agrees
AGAINST_PTS = 2.0       # prong 3: fail if the price drifted away by >= this
MIN_GAMES = 5           # prong 2: need this many completed games to judge form
FORM_N = 10
VENUE_N = 5
H2H_N = 5

_GAME_RE = re.compile(r"^(.+?)\s+@\s+(.+)$")

# Last run's funnel + near-misses, for the admin test-run page.
last_report: dict = {}


# ── odds math ────────────────────────────────────────────────────────────────

def implied(odds: int) -> float:
    return 100 / (odds + 100) if odds > 0 else abs(odds) / (abs(odds) + 100)


def profit_per_unit(odds: int) -> float:
    return odds / 100 if odds > 0 else 100 / abs(odds)


def ev_pct(p: float, odds: int) -> float:
    return (p * profit_per_unit(odds) - (1 - p)) * 100


def fmt_odds(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def sport_of(league: str) -> str:
    """Cap bucket: every soccer league shares one bucket; other leagues stand alone."""
    return "soccer" if league.startswith("soccer_") else league


def _teams_of(game: str):
    m = _GAME_RE.match((game or "").strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else (None, None)  # away, home


# ── prong 1: math ────────────────────────────────────────────────────────────

def prong_math(bet) -> dict:
    p = float(bet.adjusted_prob if bet.adjusted_prob else bet.true_prob)
    odds = int(bet.odds)
    try:
        books = len(json.loads(bet.all_book_odds)) if bet.all_book_odds else 0
    except (ValueError, TypeError):
        books = 0
    ev = ev_pct(p, odds)
    stress = ev_pct(max(p - STRESS_PTS, 0.0), odds)
    imp = implied(odds)
    passed = ev >= MIN_EV and books >= MIN_BOOKS and stress >= 0
    why = []
    if ev < MIN_EV:
        why.append(f"EV {ev:.1f}% is under the {MIN_EV:.0f}% floor")
    if books < MIN_BOOKS:
        why.append(f"only {books} book(s) price this line")
    if stress < 0:
        why.append("EV disappears if our probability is 2 points too high")
    return {
        "pass": passed, "why": why,
        "odds": odds, "decimal_profit": round(profit_per_unit(odds), 4),
        "implied_pct": round(imp * 100, 1), "true_pct": round(p * 100, 1),
        "edge_pts": round((p - imp) * 100, 1),
        "ev_pct": round(ev, 1), "stress_ev_pct": round(stress, 1), "books": books,
        "adjusted": bool(bet.adjusted_prob and abs(bet.adjusted_prob - bet.true_prob) > 0.005),
        "market_true_pct": round(float(bet.true_prob) * 100, 1),
    }


# ── prong 3: market movement ─────────────────────────────────────────────────

def prong_market(bet) -> dict:
    sig = {
        "rlm": bool(bet.rlm), "rlm_note": bet.rlm_note,
        "steam_bps": bet.steam_bps, "sharp_grade": bet.sharp_grade, "clv_grade": bet.clv_grade,
    }
    if not bet.opening_odds:
        return {"pass": False, "state": "no_data", "why": ["no opening line recorded yet"],
                "open_odds": None, "now_odds": int(bet.odds), "move_pts": None, **sig}
    move = (implied(int(bet.odds)) - implied(int(bet.opening_odds))) * 100
    if move >= AGREES_PTS:
        state, passed, why = "agrees", True, []
    elif move > -AGAINST_PTS:
        state, passed, why = "stable", True, []
    else:
        state, passed, why = "against", False, [
            f"price drifted {abs(move):.1f} pts away from this side since the open"]
    return {"pass": passed, "state": state, "why": why,
            "open_odds": int(bet.opening_odds), "now_odds": int(bet.odds),
            "move_pts": round(move, 1), **sig}


# ── prong 2: history ─────────────────────────────────────────────────────────

def _rate(hits: float, n: int) -> float:
    return hits / n if n else 0.0


def _form_lines(games, venue_home: bool):
    last = games[:FORM_N]
    venue = [g for g in games if g["home"] == venue_home][:VENUE_N]
    return last, venue


def prong_history(bet, sport_key: str) -> dict:
    away, home = _teams_of(bet.game)
    out = {"pass": False, "why": [], "lines": [], "data_ok": False}
    if not away or not home:
        out["why"].append("couldn't read the matchup")
        return out
    market = bet.market
    soccer = sport_key.startswith("soccer_")

    if market == "totals":
        a, h = team_results(sport_key, away, True), team_results(sport_key, home, True)
        if not a or not h:
            out["why"].append("team history unavailable")
            return out
        samples = [g["pf"] + g["pa"] for g in a["games"][:FORM_N]] + \
                  [g["pf"] + g["pa"] for g in h["games"][:FORM_N]]
        if len(samples) < MIN_GAMES * 2:
            out["why"].append("not enough completed games yet")
            return out
        line = float(bet.point)
        over = bet.team.lower().startswith("over")
        hit = sum(1 for t in samples if (t > line if over else t < line))
        push = sum(1 for t in samples if t == line)
        rate = _rate(hit + 0.5 * push, len(samples))
        avg = sum(samples) / len(samples)
        side = "over" if over else "under"
        out["lines"].append(
            f"Across both teams' last {FORM_N} games, {hit} of {len(samples)} finished {side} "
            f"today's {line:g} (average combined total {avg:.1f}).")
        h2h = [g for g in a["games"] if g["opp_id"] == h["team_id"]][:H2H_N]
        h2h_hit = None
        if h2h:
            h2h_hit = sum(1 for g in h2h if ((g["pf"] + g["pa"]) > line if over else (g["pf"] + g["pa"]) < line))
            out["lines"].append(
                f"Head-to-head: {h2h_hit} of the last {len(h2h)} meetings went {side} {line:g} "
                f"(average total {sum(g['pf'] + g['pa'] for g in h2h) / len(h2h):.1f}).")
        out["data_ok"] = True
        h2h_bad = len(h2h) >= 2 and h2h_hit == 0
        out["pass"] = rate >= 0.5 and not h2h_bad
        if rate < 0.5:
            out["why"].append(f"only {hit}/{len(samples)} recent games went {side} this number")
        if h2h_bad:
            out["why"].append("every recent meeting went the other way")
        out["rate"] = round(rate * 100)
        return out

    # h2h (moneyline) and spreads — a bet on a team
    team = bet.team
    is_home = team.strip().lower() == home.strip().lower()
    opp_name = away if is_home else home
    tr = team_results(sport_key, team, True)
    if not tr:
        out["why"].append("team history unavailable")
        return out
    games = tr["games"]
    last, venue = _form_lines(games, is_home)
    if len(last) < MIN_GAMES:
        out["why"].append("not enough completed games yet")
        return out
    opp = team_results(sport_key, opp_name)
    opp_id = opp["team_id"] if opp else None
    h2h = [g for g in games if opp_id and g["opp_id"] == opp_id][:H2H_N]
    where = "home" if is_home else "road"
    out["data_ok"] = True

    if market == "spreads":
        s = float(bet.point)
        cover = lambda g: (g["pf"] - g["pa"] + s)
        c10 = sum(1 for g in last if cover(g) > 0) + 0.5 * sum(1 for g in last if cover(g) == 0)
        cv = sum(1 for g in venue if cover(g) > 0) + 0.5 * sum(1 for g in venue if cover(g) == 0)
        margin = sum(g["pf"] - g["pa"] for g in last) / len(last)
        sign = f"{s:+g}"
        out["lines"].append(
            f"Against today's {sign} number, {team} would have covered {c10:g} of its last {len(last)} games "
            f"(average margin {margin:+.1f}).")
        if len(venue) >= 3:
            out["lines"].append(f"At {where}: covered {cv:g} of the last {len(venue)}.")
        if h2h:
            hc = sum(1 for g in h2h if cover(g) > 0)
            out["lines"].append(f"Head-to-head vs {opp_name}: covered {hc} of the last {len(h2h)} meetings.")
        rate = _rate(c10, len(last))
        venue_ok = len(venue) < 3 or _rate(cv, len(venue)) >= 0.5
        h2h_bad = len(h2h) >= 2 and sum(1 for g in h2h if cover(g) > 0) == 0
        out["pass"] = rate >= 0.5 and venue_ok and not h2h_bad
        if rate < 0.5:
            out["why"].append(f"covered only {c10:g}/{len(last)} recently")
        if not venue_ok:
            out["why"].append(f"poor {where} cover rate")
        if h2h_bad:
            out["why"].append("failed to cover in every recent meeting")
        out["rate"] = round(rate * 100)
        return out

    # moneyline
    def good(g):  # soccer: a draw isn't a win, so measure "unbeaten" instead
        return g["pf"] >= g["pa"] if soccer else g["pf"] > g["pa"]
    w10 = sum(1 for g in last if good(g))
    wv = sum(1 for g in venue if good(g))
    label = "unbeaten in" if soccer else "won"
    out["lines"].append(f"{team} {label} {w10} of its last {len(last)}.")
    if len(venue) >= 3:
        out["lines"].append(f"At {where}: {label} {wv} of the last {len(venue)}.")
    if h2h:
        hw = sum(1 for g in h2h if g["pf"] > g["pa"])
        out["lines"].append(f"Head-to-head vs {opp_name}: won {hw} of the last {len(h2h)} meetings.")
    bar = 0.6 if soccer else 0.5
    rate = _rate(w10, len(last))
    venue_ok = len(venue) < 3 or _rate(wv, len(venue)) >= bar
    h2h_bad = len(h2h) >= 2 and sum(1 for g in h2h if g["pf"] > g["pa"]) == 0
    out["pass"] = rate >= bar and venue_ok and not h2h_bad
    if rate < bar:
        out["why"].append(f"only {w10}/{len(last)} recently")
    if not venue_ok:
        out["why"].append(f"weak {where} record")
    if h2h_bad:
        out["why"].append("lost every recent meeting")
    out["rate"] = round(rate * 100)
    return out


# ── explanation (deterministic; LLM narration is layered on later) ───────────

def build_explanation(bet, facts: dict) -> str:
    m, h, k = facts["math"], facts["history"], facts["market"]
    parts = [
        f"The math: the market-implied win probability after removing the vig is {m['market_true_pct']}%, "
        f"while {fmt_odds(m['odds'])} only needs {m['implied_pct']}% to break even. "
        f"That {m['edge_pts']:+.1f}-point gap is worth {m['ev_pct']:+.1f}% EV per unit staked "
        f"({m['books']} books price this line). Even if we're 2 points too optimistic, it's still "
        f"{m['stress_ev_pct']:+.1f}% EV."
    ]
    if h["lines"]:
        parts.append("The history: " + " ".join(h["lines"]))
    if k["state"] == "agrees":
        parts.append(f"The market: it agrees with us. The price moved from {fmt_odds(k['open_odds'])} at the open "
                     f"to {fmt_odds(k['now_odds'])} now ({k['move_pts']:+.1f} pts toward this side).")
    elif k["state"] == "stable":
        parts.append(f"The market: steady. {fmt_odds(k['open_odds'])} at the open, "
                     f"{fmt_odds(k['now_odds'])} now — no sharp move against us.")
    if k.get("rlm_note"):
        parts.append(f"Sharp signal: {k['rlm_note']}.")
    return "\n\n".join(parts)


# ── selection ────────────────────────────────────────────────────────────────

def _bet_key(b):
    return (b.game_id or b.game, b.market, b.team, b.point)


def _candidates(db, now_utc, window_end):
    rows = (
        db.query(EVBetCache)
        .filter(
            EVBetCache.commence_time > now_utc,
            EVBetCache.commence_time < window_end,
            EVBetCache.market.in_(("h2h", "spreads", "totals")),
            (EVBetCache.is_prop.is_(False)) | (EVBetCache.is_prop.is_(None)),
            EVBetCache.ev_percent >= MIN_EV,
            EVBetCache.true_prob >= MIN_TRUE_PROB,
            EVBetCache.team != "Draw",
        )
        .order_by(EVBetCache.ev_percent.desc())
        .limit(250)
        .all()
    )
    best = {}
    for r in rows:                      # best price per exact bet
        best.setdefault(_bet_key(r), r)
    return list(best.values())


def run_edition(edition_date=None, persist: bool = True) -> dict:
    """
    Build today's edition from the current EVBetCache. Writes shadow Pick rows
    (replacing any earlier shadow run for the date) and returns a report.
    """
    from web.newsletter import _composite_pick_score

    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc.astimezone(_CT)
    edition_date = edition_date or now_ct.date()
    window_end = (datetime(now_ct.year, now_ct.month, now_ct.day, tzinfo=_CT)
                  + timedelta(days=1, hours=5)).astimezone(timezone.utc)

    db = SessionLocal()
    try:
        cands = _candidates(db, now_utc, window_end)
        funnel = {"candidates": len(cands), "math": 0, "market": 0, "history": 0, "all_three": 0}
        evaluated, qualified = [], []
        for bet in cands:
            m = prong_math(bet)
            k = prong_market(bet)
            row = {"bet": bet, "math": m, "market": k, "history": None}
            if m["pass"]:
                funnel["math"] += 1
            if k["pass"]:
                funnel["market"] += 1
            if m["pass"] and k["pass"]:      # only spend ESPN calls on likely winners
                try:
                    row["history"] = prong_history(bet, bet.league)
                except Exception as exc:
                    log.warning("history prong failed for %s: %s", bet.game, exc)
                    row["history"] = {"pass": False, "why": ["history lookup error"], "lines": [], "data_ok": False}
                if row["history"]["pass"]:
                    funnel["history"] += 1
                    funnel["all_three"] += 1
                    row["score"] = _composite_pick_score(bet)
                    qualified.append(row)
            evaluated.append(row)

        # One pick per game; best score wins.
        qualified.sort(key=lambda r: r["score"], reverse=True)
        seen_games, unique = set(), []
        for r in qualified:
            gk = r["bet"].game_id or r["bet"].game
            if gk in seen_games:
                continue
            seen_games.add(gk)
            unique.append(r)

        # Diversity first: best pick of each sport, then fill by score.
        chosen, per_sport = [], {}
        for r in unique:
            sp = sport_of(r["bet"].league)
            if per_sport.get(sp, 0) == 0 and len(chosen) < TOTAL_CAP:
                chosen.append(r)
                per_sport[sp] = 1
        for r in unique:
            sp = sport_of(r["bet"].league)
            if r in chosen or len(chosen) >= TOTAL_CAP or per_sport.get(sp, 0) >= PER_SPORT_CAP:
                continue
            chosen.append(r)
            per_sport[sp] = per_sport.get(sp, 0) + 1
        chosen.sort(key=lambda r: r["score"], reverse=True)

        picks = []
        for i, r in enumerate(chosen, 1):
            b = r["bet"]
            facts = {"math": r["math"], "history": r["history"], "market": r["market"]}
            picks.append(Pick(
                edition_date=edition_date, status="shadow", is_potd=(i == 1), rank=i,
                league=b.league, market=b.market, team=b.team, game=b.game, point=b.point,
                book=b.book, odds=int(b.odds), game_id=b.game_id, commence_time=b.commence_time,
                source_type=b.source_type or "sportsbook",
                ev_percent=round(r["math"]["ev_pct"], 2), true_prob=float(b.adjusted_prob or b.true_prob),
                opening_odds=b.opening_odds,
                prong_math=True, prong_history=True, prong_market=True,
                facts=json.dumps(facts), explanation=build_explanation(b, facts),
            ))
        near = []
        for r in evaluated:
            if r in chosen:
                continue
            passed = sum(1 for x in (r["math"], r["market"], r["history"]) if x and x["pass"])
            reasons = (r["math"]["why"] + r["market"]["why"] + ((r["history"] or {}).get("why") or []))
            near.append({
                "game": r["bet"].game, "league": r["bet"].league, "market": r["bet"].market,
                "team": r["bet"].team, "point": r["bet"].point, "odds": int(r["bet"].odds),
                "ev": round(r["math"]["ev_pct"], 1), "prongs_passed": passed,
                "math": r["math"]["pass"], "market_ok": r["market"]["pass"],
                "history": (r["history"] or {}).get("pass") if r["history"] else None,
                "reasons": reasons[:3],
            })
        near.sort(key=lambda x: (-x["prongs_passed"], -x["ev"]))

        report = {
            "edition_date": str(edition_date), "ran_at": now_utc.isoformat(),
            "funnel": funnel, "selected": len(picks),
            "by_sport": dict(per_sport),
            "near_misses": near[:25],
        }
        if persist:
            db.query(Pick).filter(Pick.edition_date == edition_date, Pick.status == "shadow").delete()
            db.add_all(picks)
            db.commit()

        last_report.clear()
        last_report.update(report)
        log.info("Shadow edition %s: %s -> %d picks", edition_date, funnel, len(picks))
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
