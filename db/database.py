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
    DateTime,
    Float,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.getenv("DATABASE_URL") or "sqlite:///./sports_ev.db"

# SQLite needs check_same_thread=False for FastAPI's threaded request handling
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

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
    league     = Column(String, index=True, nullable=False)   # e.g. "icehockey_nhl"
    market     = Column(String, nullable=False)               # e.g. "h2h", "spreads"
    team       = Column(String, nullable=False)               # outcome_name
    book       = Column(String, nullable=False)               # bookmaker
    ev_percent = Column(Float, nullable=False)                # EV% (effective_ev_pct if available)
    true_prob  = Column(Float, nullable=False)                # no-vig true probability
    odds       = Column(Integer, nullable=False)              # American odds
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True, nullable=False)

    def __repr__(self) -> str:
        sign = "+" if self.odds > 0 else ""
        return (
            f"<EVBetCache id={self.id} league={self.league!r} "
            f"team={self.team!r} odds={sign}{self.odds} ev={self.ev_percent:.1f}%>"
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
