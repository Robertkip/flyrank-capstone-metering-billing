"""Data layer: tenants, plans, subscriptions, usage events, webhook inbox."""
from datetime import datetime, timezone
from sqlalchemy import (JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
                        create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import settings


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Plan(Base):
    __tablename__ = "plans"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    api_calls_limit: Mapped[int] = mapped_column(BigInteger)
    ai_tokens_limit: Mapped[int] = mapped_column(BigInteger)
    base_fee_micros: Mapped[int] = mapped_column(BigInteger)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)   # sha256 of the API key; raw key never stored
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"), default="free")
    status: Mapped[str] = mapped_column(String(20), default="active")    # active | past_due
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String(64), unique=True)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"))
    status: Mapped[str] = mapped_column(String(30))                      # Stripe's status, mirrored
    last_event_created: Mapped[int] = mapped_column(BigInteger, default=0)  # ignore out-of-order older events
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class UsageEvent(Base):
    """One billable action. (tenant_id, idempotency_key) is UNIQUE: the database itself forbids double counting."""
    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    period: Mapped[str] = mapped_column(String(7))                       # YYYY-MM (UTC)
    api_calls: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    quota_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)     # per-event estimate (display)
    response: Mapped[dict] = mapped_column(JSON)                         # replayed verbatim on retries
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_idem"),
                      Index("ix_usage_tenant_period", "tenant_id", "period"))


class WebhookEvent(Base):
    """Inbox of verified Stripe events. The unique event id makes replays no-ops."""
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stripe_event_id: Mapped[str] = mapped_column(String(80), unique=True)
    type: Mapped[str] = mapped_column(String(80))
    created: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="received")  # received | processed | ignored | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_webhook_status", "status"),)


engine = create_engine(settings.database_url, future=True,
                       connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
