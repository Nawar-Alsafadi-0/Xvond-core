from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    validates,
)

from backend.app.core.database.base import Base


class AgentChannel(Base):
    __tablename__ = "agent_channels"

    __table_args__ = (
        Index(
            "uq_agent_channels_agent_type",
            "agent_id",
            "channel_type",
            unique=True,
        ),
        Index(
            "ix_agent_channels_company_enabled",
            "company_id",
            "enabled",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
    )

    agent_id: Mapped[int] = mapped_column(
        ForeignKey("ai_agents.id"),
        nullable=False,
        index=True,
    )

    channel_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    config: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
        nullable=False,
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # System-owned runtime acceptance evidence. These fields are deliberately
    # outside ``config`` so admin/customer config APIs cannot forge customer
    # readiness by injecting a timestamp into mutable channel configuration.
    customer_roundtrip_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    customer_roundtrip_source: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    @validates("config")
    def protect_stored_config(self, _key, value):
        from backend.app.core.config_secrets import protect_config
        return protect_config(value or {})

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )



class ManagedChannelOutboundDelivery(Base):
    """Durable provider-neutral delivery state for Xvond-managed channels."""

    __tablename__ = "managed_channel_outbound_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_managed_channel_outbound_idempotency",
        ),
        Index(
            "ix_managed_channel_delivery_company_status",
            "company_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[int] = mapped_column(
        ForeignKey("ai_agents.id"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("ai_conversations.id"),
        nullable=False,
        index=True,
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("agent_channels.id"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("ai_messages.id"),
        nullable=False,
        index=True,
    )
    external_contact_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    inbound_external_message_id: Mapped[str | None] = mapped_column(
        String(180),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
        nullable=False,
        index=True,
    )
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(160), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
