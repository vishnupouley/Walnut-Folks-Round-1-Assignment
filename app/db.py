"""Database engine, session factory and the transactions table."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import DateTime, Numeric, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

IST = timezone(timedelta(hours=5, minutes=30))


def istnow() -> datetime:
    """Current time in Indian Standard Time (IST)."""
    return datetime.now(IST)


BASE_DIR = Path(__file__).resolve().parent.parent

# Postgres in production (managed providers hand out "postgres://" URLs,
# SQLAlchemy needs the driver-qualified scheme). SQLite by default so the
# service runs locally with zero setup.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite:///{BASE_DIR / 'transactions.db'}"
)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    # SQLite is file-locked: allow concurrent writers to wait instead of failing.
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)


SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Unique constraint is the single source of truth for idempotency.
    transaction_id: Mapped[str] = mapped_column(String(128), unique=True)
    source_account: Mapped[str] = mapped_column(String(128))
    destination_account: Mapped[str] = mapped_column(String(128))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session():
    """FastAPI dependency: one session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
