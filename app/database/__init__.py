"""
database – SQLAlchemy models and session management.

Tables
------
domains          Core domain metadata (name, TLD, expiry date, status, …)
domain_metrics   SEO / valuation snapshot (DA, estimated value, backlinks)
snipe_attempts   Audit log for every registration attempt made by the sniper
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


# ─── Enumerations ─────────────────────────────────────────────────────────────

class DomainStatus(str, enum.Enum):
    """Lifecycle stages we care about for a domain."""
    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"      # < 30 days to expiry
    REDEMPTION = "redemption"            # grace period after expiry
    PENDING_DELETE = "pending_delete"    # ~5 days before the domain drops
    AVAILABLE = "available"              # successfully dropped / unregistered
    SNIPED = "sniped"                   # we registered it
    FLAGGED_TRADEMARK = "flagged_trademark"


class SnipeResult(str, enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    ALREADY_REGISTERED = "already_registered"


# ─── ORM Base ─────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ─── Models ───────────────────────────────────────────────────────────────────

class Domain(Base):
    """Core domain record."""

    __tablename__ = "domains"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(253), nullable=False, unique=True, index=True)
    tld = Column(String(63), nullable=False, index=True)

    # Lifecycle
    registered_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(
        Enum(DomainStatus),
        nullable=False,
        default=DomainStatus.ACTIVE,
        index=True,
    )

    # Trademark safety
    trademark_flagged = Column(Boolean, nullable=False, default=False)
    trademark_reason = Column(Text, nullable=True)

    # Housekeeping
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class DomainMetrics(Base):
    """SEO / valuation data for a domain (one row per snapshot)."""

    __tablename__ = "domain_metrics"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    domain_id = Column(BigInteger, nullable=False, index=True)

    domain_authority = Column(Float, nullable=True)   # Moz DA (0-100)
    estimated_value_usd = Column(Float, nullable=True)
    backlink_count = Column(Integer, nullable=True)
    referring_domains = Column(Integer, nullable=True)

    snapshot_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SnipeAttempt(Base):
    """Audit record for every registration attempt."""

    __tablename__ = "snipe_attempts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    domain_id = Column(BigInteger, nullable=False, index=True)

    registrar = Column(String(64), nullable=False)   # "dynadot" | "namejet"
    result = Column(Enum(SnipeResult), nullable=False)
    error_message = Column(Text, nullable=True)

    attempted_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ─── Engine / Session factory ─────────────────────────────────────────────────

def get_engine():
    return create_engine(settings.database_url, pool_pre_ping=True)


def get_session_factory(engine=None):
    if engine is None:
        engine = get_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine=None) -> None:
    """Create all tables (idempotent)."""
    if engine is None:
        engine = get_engine()
    Base.metadata.create_all(engine)
