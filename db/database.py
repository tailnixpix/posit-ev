"""
db/database.py — SQLAlchemy models and database setup.

Models:
    User                  — registered web users with Stripe subscription info
    NewsletterSubscriber  — email-only newsletter signups
    EVBetCache            — cached +EV bet results from pipeline runs

Usage:
    from db.database import get_db, create_tables, User, NewsletterSubscriber, EVBetCache

    # FastAPI dependency injection
    @app.get("/bets")
    def get_bets(db: Session = Depends(get_db)):
        return db.query(EVBetCache).all()

    # Standalone (e.g. scripts)
    create_tables()
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_raw_url = os.getenv("DATABASE_URL")

if _raw_url:
    # Normalise any postgres:// or postgresql:// to postgresql+psycopg2://
    DATABASE_URL = _raw_url
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
    # Replace any stale pg8000 driver reference
    DATABASE_URL = DATABASE_URL.replace("+pg8000", "+psycopg2")
    connect_args = {}
else:
    DATABASE_URL = "sqlite:///./positplusev.db"
    connect_args = {"check_same_thread": False}

print(f"[db] DATABASE_URL: {DATABASE_URL[:30]}...")

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id                     = Column(Integer, primary_key=True, index=True)
    email                  = Column(String, unique=True, index=True, nullable=False)
    hashed_password        = Column(String, nullable=False)
    is_subscribed          = Column(Boolean, default=False, nullable=False)
    stripe_customer_id     = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    created_at             = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} subscribed={self.is_subscribed}>"


class NewsletterSubscriber(Base):
    __tablename__ = "newsletter_subscribers"

    id            = Column(Integer, primary_key=True, index=True)
    email         = Column(String, unique=True, index=True, nullable=False)
    subscribed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    is_active     = Column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<NewsletterSubscriber id={self.id} email={self.email!r} active={self.is_active}>"


class EVBetCache(Base):
    """Stores a snapshot of +EV bets from the most recent pipeline run."""

    __tablename__ = "ev_bet_cache"

    id         = Column(Integer, primary_key=True, index=True)
    game_id       = Column(String, nullable=True, index=True)   # Odds API game ID (for OddsHistory lookups)
    league     = Column(String, index=True, nullable=False)   # e.g. "icehockey_nhl"
    market     = Column(String, nullable=False)               # e.g. "h2h", "spreads"
    team       = Column(String, nullable=False)               # outcome_name
    game          = Column(String, nullable=True)             # e.g. "Bruins @ Maple Leafs"
    point         = Column(Float, nullable=True)              # spread/total line value
    commence_time = Column(DateTime(timezone=True), nullable=True)  # game start time (UTC)
    book          = Column(String, nullable=False)            # bookmaker
    source_type   = Column(String, nullable=True, default="sportsbook")  # "sportsbook" or "prediction_market"
    ev_percent    = Column(Float, nullable=False)              # EV% (effective_ev_pct if available)
    true_prob     = Column(Float, nullable=False)             # no-vig true probability (sharp market consensus)
    adjusted_prob = Column(Float, nullable=True)              # prob after sport-specific context adjustments
    adj_flags     = Column(String, nullable=True)             # pipe-separated adjustment labels shown as pills
    implied_prob  = Column(Float, nullable=True)              # book's raw implied probability (vig-on)
    opening_odds  = Column(Integer, nullable=True)            # first recorded odds for this bet from OddsHistory
    odds          = Column(Integer, nullable=False)           # American odds
    player_name   = Column(String, nullable=True)             # player name for prop bets
    is_prop       = Column(Boolean, nullable=True, default=False)  # True for player prop bets
    analysis             = Column(Text, nullable=True)              # AI natural language analysis
    analysis_generated_at = Column(DateTime(timezone=True), nullable=True)  # when analysis was generated
    confidence_score     = Column(Float, nullable=True)             # AI confidence 1-100
    kelly_pct            = Column(Float, nullable=True)             # 25% fractional Kelly %
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True, nullable=False)

    def __repr__(self) -> str:
        sign = "+" if self.odds > 0 else ""
        return (
            f"<EVBetCache id={self.id} league={self.league!r} "
            f"team={self.team!r} odds={sign}{self.odds} ev={self.ev_percent:.1f}%>"
        )


class DailyPick(Base):
    """
    Permanent snapshot of the morning newsletter pick for each CT calendar day.
    Written once at 8 AM when the newsletter sends. Never deleted — builds a
    historical track record and powers the pinned dashboard card.
    """

    __tablename__ = "daily_picks"

    id            = Column(Integer, primary_key=True, index=True)
    pick_date     = Column(Date, unique=True, index=True, nullable=False)  # CT date e.g. 2026-03-29
    league        = Column(String, nullable=True)
    market        = Column(String, nullable=True)
    team          = Column(String, nullable=True)
    game          = Column(String, nullable=True)
    point         = Column(Float, nullable=True)
    book          = Column(String, nullable=True)
    source_type   = Column(String, nullable=True, default="sportsbook")
    ev_percent    = Column(Float, nullable=True)
    true_prob     = Column(Float, nullable=True)
    odds          = Column(Integer, nullable=True)
    commence_time = Column(DateTime(timezone=True), nullable=True)
    synopsis      = Column(Text, nullable=True)
    sent_at       = Column(DateTime(timezone=True), nullable=True)
    result        = Column(String, nullable=True)  # "won"|"lost"|"push"|"pending" — future use
    game_id       = Column(String, nullable=True)  # Odds API game ID — used for CLV closing line lookup

    def __repr__(self) -> str:
        return (
            f"<DailyPick date={self.pick_date} team={self.team!r} "
            f"ev={self.ev_percent}% book={self.book!r}>"
        )


class OddsHistory(Base):
    """
    Append-only ledger of every +EV bet seen at each hourly snapshot.
    Used to compute opening line (first seen) and closing line (last seen
    before commence_time) for CLV calculations.
    """

    __tablename__ = "odds_history"

    id            = Column(Integer, primary_key=True, index=True)
    game_id       = Column(String,  nullable=False, index=True)
    league        = Column(String,  nullable=False)
    market        = Column(String,  nullable=False)
    team          = Column(String,  nullable=False)
    game          = Column(String,  nullable=True)
    point         = Column(Float,   nullable=True)
    book          = Column(String,  nullable=False)
    odds          = Column(Integer, nullable=False)
    implied_prob  = Column(Float,   nullable=True)   # vig-inclusive book probability
    true_prob     = Column(Float,   nullable=True)   # sharp no-vig consensus probability
    ev_percent    = Column(Float,   nullable=True)
    commence_time = Column(DateTime(timezone=True), nullable=True)
    captured_at   = Column(DateTime(timezone=True), nullable=False, index=True)

    def __repr__(self) -> str:
        return (
            f"<OddsHistory game_id={self.game_id!r} book={self.book!r} "
            f"team={self.team!r} odds={self.odds} at={self.captured_at}>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_tables() -> None:
    """Create all tables (no-op if they already exist)."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """
    FastAPI dependency that yields a SQLAlchemy Session and ensures it is
    closed after the request completes.

    Usage:
        @app.get("/users/{user_id}")
        def read_user(user_id: int, db: Session = Depends(get_db)):
            return db.query(User).filter(User.id == user_id).first()
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Standalone init
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    create_tables()
    print(f"Tables created (or already exist) — database: {DATABASE_URL}")
