from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database.base import Base


class ServicePlan(Base):
    __tablename__ = "service_plans"
    __table_args__ = (
        UniqueConstraint("service_code", "tier", name="uq_service_plans_service_tier"),
        Index("ix_service_plans_service_enabled", "service_code", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_code: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    monthly_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), default=Decimal("0"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(10), default="OMR", nullable=False)
    limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class ServiceSubscription(Base):
    __tablename__ = "service_subscriptions"
    __table_args__ = (
        UniqueConstraint("company_id", "service_code", name="uq_service_subscription_company_service"),
        Index("ix_service_subscriptions_company_status", "company_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    service_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("service_plans.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    current_period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class ServiceUsageEvent(Base):
    __tablename__ = "service_usage_events"
    __table_args__ = (
        Index(
            "ix_service_usage_company_service_metric_created",
            "company_id",
            "service_code",
            "metric",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    service_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 3), default=Decimal("1"), nullable=False
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)



class ServiceCheckout(Base):
    __tablename__ = "service_checkouts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_transaction_id",
            name="uq_service_checkouts_provider_transaction",
        ),
        Index(
            "ix_service_checkouts_company_status",
            "company_id",
            "status",
        ),
        Index(
            "ix_service_checkouts_subscription_plan",
            "service_subscription_id",
            "plan_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
    )
    service_subscription_id: Mapped[int] = mapped_column(
        ForeignKey("service_subscriptions.id"),
        nullable=False,
        index=True,
    )
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("service_plans.id"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_transaction_id: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 3),
        default=Decimal("0"),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ServicePaymentEvent(Base):
    __tablename__ = "service_payment_events"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_service_payment_events_provider_event",
        ),
        Index(
            "ix_service_payment_events_provider_created",
            "provider",
            "created_at",
        ),
        Index(
            "ix_service_payment_events_company_type",
            "company_id",
            "event_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"),
        nullable=True,
        index=True,
    )
    service_checkout_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_checkouts.id"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
