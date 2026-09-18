"""add durable managed channel outbound deliveries

Revision ID: d4e8a2c7f190
Revises: b91f2d6a4e70
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e8a2c7f190"
down_revision = "b91f2d6a4e70"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "managed_channel_outbound_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=320), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("ai_agents.id"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("ai_conversations.id"), nullable=False),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("agent_channels.id"), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("ai_messages.id"), nullable=False),
        sa.Column("external_contact_id", sa.String(length=200), nullable=False),
        sa.Column("inbound_external_message_id", sa.String(length=180), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error_code", sa.String(length=160), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_managed_channel_outbound_idempotency",
        ),
    )
    op.create_index(
        "ix_managed_channel_delivery_company_status",
        "managed_channel_outbound_deliveries",
        ["company_id", "status"],
        unique=False,
    )
    for name, columns in (
        ("ix_managed_channel_outbound_deliveries_idempotency_key", ["idempotency_key"]),
        ("ix_managed_channel_outbound_deliveries_company_id", ["company_id"]),
        ("ix_managed_channel_outbound_deliveries_agent_id", ["agent_id"]),
        ("ix_managed_channel_outbound_deliveries_conversation_id", ["conversation_id"]),
        ("ix_managed_channel_outbound_deliveries_channel_id", ["channel_id"]),
        ("ix_managed_channel_outbound_deliveries_message_id", ["message_id"]),
        ("ix_managed_channel_outbound_deliveries_external_contact_id", ["external_contact_id"]),
        ("ix_managed_channel_outbound_deliveries_inbound_external_message_id", ["inbound_external_message_id"]),
        ("ix_managed_channel_outbound_deliveries_status", ["status"]),
        ("ix_managed_channel_outbound_deliveries_provider_message_id", ["provider_message_id"]),
    ):
        op.create_index(name, "managed_channel_outbound_deliveries", columns, unique=False)


def downgrade():
    for name in (
        "ix_managed_channel_outbound_deliveries_provider_message_id",
        "ix_managed_channel_outbound_deliveries_status",
        "ix_managed_channel_outbound_deliveries_inbound_external_message_id",
        "ix_managed_channel_outbound_deliveries_external_contact_id",
        "ix_managed_channel_outbound_deliveries_message_id",
        "ix_managed_channel_outbound_deliveries_channel_id",
        "ix_managed_channel_outbound_deliveries_conversation_id",
        "ix_managed_channel_outbound_deliveries_agent_id",
        "ix_managed_channel_outbound_deliveries_company_id",
        "ix_managed_channel_outbound_deliveries_idempotency_key",
        "ix_managed_channel_delivery_company_status",
    ):
        op.drop_index(name, table_name="managed_channel_outbound_deliveries")
    op.drop_table("managed_channel_outbound_deliveries")
