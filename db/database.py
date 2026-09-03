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
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
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
    trial_ends_at          = Column(DateTime(timezone=True), nullable=True)
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
    bet_pct       = Column(Float, nullable=True)             # % of bets on this side (Action Network)
    money_pct     = Column(Float, nullable=True)             # % of money on this side (handle %)
    sharp_score   = Column(Float, nullable=True)             # 0-100 sharp money signal
    analysis             = Column(Text, nullable=True)              # AI natural language analysis
    analysis_generated_at = Column(DateTime(timezone=True), nullable=True)  # when analysis was generated
    confidence_score     = Column(Float, nullable=True)             # AI confidence 1-100
    kelly_pct            = Column(Float, nullable=True)             # 25% fractional Kelly %
    # Model projection snapshot (stored at pipeline time so cards load instantly)
    proj_away_score      = Column(Float, nullable=True)   # model projected away team score
    proj_home_score      = Column(Float, nullable=True)   # model projected home team score
    proj_total           = Column(Float, nullable=True)   # model projected total
    proj_home_win_prob   = Column(Float, nullable=True)   # model home win probability
    proj_away_display    = Column(String, nullable=True)  # away team name from projection source
    proj_home_display    = Column(String, nullable=True)  # home team name from projection source
    home_trend           = Column(String, nullable=True)   # e.g. "7-3 W4"
    away_trend           = Column(String, nullable=True)
    all_book_odds        = Column(Text, nullable=True)     # JSON: {bookmaker: american_odds} for all books on this line
    card_summary         = Column(Text, nullable=True)     # Short 2-3 sentence AI summary shown inline on card
    game_context         = Column(Text, nullable=True)     # JSON: real-world enrichment (injuries, rest, weather, pace)
    # Sharp-signal columns (computed at pipeline time by scripts/sharp_signals.py)
    rlm              = Column(Boolean, nullable=True, default=False)  # Reverse Line Movement detected
    rlm_note         = Column(String, nullable=True)    # e.g. "72% public against · line moved +110→+130"
    steam_bps        = Column(Integer, nullable=True)   # basis points line moved toward this bet since open
    line_shop_bps    = Column(Integer, nullable=True)   # bps better than market average across all books
    clv_grade        = Column(String, nullable=True)    # A/B/C/D/F — closing line value potential
    sharp_grade      = Column(String, nullable=True)    # S/A/B/C/D — composite conviction grade
    pred_mkt_note    = Column(String, nullable=True)    # e.g. "Kalshi: 62% · Polymarket: 64%"
    pred_mkt_aligned = Column(Boolean, nullable=True, default=False)  # prediction markets agree with model
    injury_alert     = Column(Text, nullable=True)      # e.g. "Jaylen Brown (SG) Out — Knee"
    hr_model_prob   = Column(Float, nullable=True)   # HR model: true P(HR in game)
    hr_model_score  = Column(Float, nullable=True)   # HR model: composite ranking score
    hr_model_meta   = Column(Text, nullable=True)    # HR model: JSON {pitcher_name, pitcher_hr9, park_label, wind_label, batter_hr_ppa, batter_pa}
    odds_updated_at = Column(DateTime(timezone=True), nullable=True)  # when odds were last fetched from the Odds API
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True, nullable=False)

    __table_args__ = (
        Index('ix_ev_bet_cache_commence_time', 'commence_time'),
        Index('ix_ev_bet_cache_ev_percent', 'ev_percent'),
    )

    def __repr__(self) -> str:
        sign = "+" if self.odds > 0 else ""
        return (
            f"<EVBetCache id={self.id} league={self.league!r} "
            f"team={self.team!r} odds={sign}{self.odds} ev={self.ev_percent:.1f}%>"
        )


class WatchlistEntry(Base):
    """User's paper-trail watchlist of +EV bets they're tracking without real money."""

    __tablename__ = 'watchlist_entries'

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey('users.id'), nullable=False)
    bet_cache_id  = Column(Integer, nullable=True)  # intentionally no FK — ev_bet_cache rows are ephemeral
    game          = Column(String, nullable=True)
    league        = Column(String, nullable=True)
    team          = Column(String, nullable=True)
    market        = Column(String, nullable=True)
    odds          = Column(Integer, nullable=True)
    ev_percent    = Column(Float, nullable=True)
    true_prob     = Column(Float, nullable=True)
    added_at      = Column(DateTime(timezone=True), server_default=func.now())
    notes         = Column(String, nullable=True)
    paper_result  = Column(String, nullable=True, default='pending')
    paper_odds    = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<WatchlistEntry id={self.id} user_id={self.user_id} team={self.team!r} result={self.paper_result!r}>"


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
    player_name   = Column(String, nullable=True)  # player name for prop picks
    is_prop       = Column(Boolean, nullable=True, default=False)

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


class GameSimulation(Base):
    """
    Pre-computed Monte Carlo simulation results for a game.

    One row per unique (sport_key, game) pair.  Written by the background
    simulation job that runs after each EV cache refresh.  The dashboard
    reads from this table at page-load time so no live computation is needed.
    """

    __tablename__ = "game_simulations"

    id              = Column(Integer, primary_key=True, index=True)
    sport_key       = Column(String, nullable=False, index=True)   # e.g. "baseball_mlb"
    game_id         = Column(String, nullable=True)                 # Odds API event ID
    game            = Column(String, nullable=False, index=True)   # "Away @ Home"
    home_team       = Column(String, nullable=False)
    away_team       = Column(String, nullable=False)
    commence_time   = Column(DateTime(timezone=True), nullable=True)
    n_sims          = Column(Integer, default=10_000)
    home_win_pct    = Column(Float, nullable=True)    # % of sims home team won
    away_win_pct    = Column(Float, nullable=True)    # % of sims away team won
    draw_pct        = Column(Float, nullable=True)    # % of sims ending in draw (soccer)
    projected_outcome = Column(String, nullable=True) # "Home Win" | "Away Win" | "Draw"
    confidence      = Column(Float, nullable=True)    # 0–100 — pct of sims matching projected_outcome
    avg_home_score  = Column(Float, nullable=True)    # mean projected home runs/goals
    avg_away_score  = Column(Float, nullable=True)    # mean projected away runs/goals
    summary         = Column(Text, nullable=True)     # AI-generated narrative explanation
    sim_data        = Column(Text, nullable=True)     # JSON with full breakdown (over/under pct, etc.)
    updated_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<GameSimulation sport={self.sport_key!r} game={self.game!r} "
            f"outcome={self.projected_outcome!r} confidence={self.confidence}%>"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_tables() -> None:
    """Create all tables (no-op if they already exist)."""
    Base.metadata.create_all(bind=engine)


def ensure_columns() -> None:
    """
    Non-destructive migration: add any columns that exist in the SQLAlchemy
    models but are missing from the live database table.

    Safe to run on every startup — uses IF NOT EXISTS so it is a no-op when
    all columns are already present.  This handles the common case where the
    production table was created from an older version of the model before new
    columns were added.

    Only works for nullable or default-having columns (which all our newer
    columns are).  SQLite doesn't support IF NOT EXISTS in ALTER TABLE, so we
    skip the check on SQLite.
    """
    import sqlalchemy as _sa

    is_sqlite = DATABASE_URL.startswith("sqlite")
    if is_sqlite:
        return  # SQLite ALTER TABLE is too limited; handled by create_all instead

    from sqlalchemy import inspect as _inspect, text as _text

    try:
        inspector = _inspect(engine)
        # Map SQLAlchemy column types to SQL type strings
        _type_map = {
            "INTEGER":   "INTEGER",
            "VARCHAR":   "TEXT",
            "TEXT":      "TEXT",
            "BOOLEAN":   "BOOLEAN",
            "FLOAT":     "DOUBLE PRECISION",
            "NUMERIC":   "DOUBLE PRECISION",
            "DATETIME":  "TIMESTAMPTZ",
            "DATE":      "DATE",
        }

        for table_name, model_class in [
            ("ev_bet_cache",          EVBetCache),
            ("daily_picks",           DailyPick),
            ("odds_history",          OddsHistory),
            ("users",                 User),
            ("newsletter_subscribers", NewsletterSubscriber),
            ("game_simulations",      GameSimulation),
        ]:
            try:
                existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
            except Exception:
                continue  # table doesn't exist yet — create_tables handles this

            for col in model_class.__table__.columns:
                if col.name in existing_cols:
                    continue
                col_type = type(col.type).__name__.upper()
                sql_type = _type_map.get(col_type, "TEXT")
                # Append timezone for timestamp columns
                if col_type == "DATETIME":
                    sql_type = "TIMESTAMPTZ"
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            _text(f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{col.name}" {sql_type}')
                        )
                    print(f"[db] Added missing column: {table_name}.{col.name} ({sql_type})")
                except Exception as _exc:
                    print(f"[db] Warning: could not add {table_name}.{col.name}: {_exc}")

        # Ensure unique index on newsletter_subscribers.email (guards against
        # tables created before the unique=True constraint was in the model).
        try:
            with engine.begin() as conn:
                conn.execute(_text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS"
                    " ix_newsletter_subscribers_email_unique"
                    " ON newsletter_subscribers (email)"
                ))
        except Exception as _exc:
            print(f"[db] Warning: could not ensure newsletter email unique index: {_exc}")

    except Exception as _top:
        print(f"[db] ensure_columns failed (non-fatal): {_top}")


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
