"""
web/record.py — one place for win/loss/unit math, so the newsletter, public
pages, admin and track-record page can never disagree.

The record is flat 1-unit staking; a unit is UNIT_DOLLARS ($20 on the $1,000
bankroll the site has always used). The record spans the legacy single daily
picks (DailyPick) plus every published multi-pick edition (Pick).
"""
from db.database import DailyPick, Pick

UNIT_DOLLARS = 20.0


def pick_units(p) -> float:
    """Profit of a settled pick in units (1u flat stake)."""
    if p.result == "won" and p.odds:
        return p.odds / 100 if p.odds > 0 else 100 / abs(p.odds)
    if p.result == "lost":
        return -1.0
    return 0.0


def bucket(picks: list) -> dict:
    won = sum(1 for p in picks if p.result == "won")
    lost = sum(1 for p in picks if p.result == "lost")
    push = sum(1 for p in picks if p.result == "push")
    units = round(sum(pick_units(p) for p in picks), 2)
    staked = won + lost
    clvs = [p.clv_pct for p in picks if p.clv_pct is not None]
    return {
        "won": won, "lost": lost, "push": push, "total": len(picks),
        "units": units, "dollars": round(units * UNIT_DOLLARS, 2),
        "roi": round(units / staked * 100, 1) if staked else None,
        "win_pct": round(won / staked * 100, 1) if staked else None,
        "clv_n": len(clvs),
        "avg_clv": round(sum(clvs) / len(clvs), 2) if clvs else None,
        "clv_beat_pct": round(sum(1 for c in clvs if c > 0) / len(clvs) * 100) if clvs else None,
    }


def settled_picks(db, include_shadow: bool = False) -> list:
    """Every graded pick that counts toward the public record, oldest first."""
    legacy = db.query(DailyPick).filter(DailyPick.result.in_(("won", "lost", "push"))).all()
    statuses = ("published", "shadow") if include_shadow else ("published",)
    new = (
        db.query(Pick)
        .filter(Pick.status.in_(statuses), Pick.result.in_(("won", "lost", "push")))
        .all()
    )
    day = lambda p: getattr(p, "pick_date", None) or p.edition_date
    return sorted(legacy + new, key=day)
