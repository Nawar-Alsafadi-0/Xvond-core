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
            name="uq_mco_delivery_idempotency",
        ),
    )
    op.create_index(
        "ix_mco_delivery_company_status",
        "managed_channel_outbound_deliveries",
        ["company_id", "status"],
        unique=False,
    )
    for name, columns in (
        ("ix_mco_delivery_idempotency", ["idempotency_key"]),
        ("ix_mco_delivery_company", ["company_id"]),
        ("ix_mco_delivery_agent", ["agent_id"]),
        ("ix_mco_delivery_conversation", ["conversation_id"]),
        ("ix_mco_delivery_channel", ["channel_id"]),
        ("ix_mco_delivery_message", ["message_id"]),
        ("ix_mco_delivery_contact", ["external_contact_id"]),
        ("ix_mco_delivery_inbound", ["inbound_external_message_id"]),
        ("ix_mco_delivery_status", ["status"]),
        ("ix_mco_delivery_provider_message", ["provider_message_id"]),
    ):
        op.create_index(name, "managed_channel_outbound_deliveries", columns, unique=False)


def downgrade():
    for name in (
        "ix_mco_delivery_provider_message",
        "ix_mco_delivery_status",
        "ix_mco_delivery_inbound",
        "ix_mco_delivery_contact",
        "ix_mco_delivery_message",
        "ix_mco_delivery_channel",
        "ix_mco_delivery_conversation",
        "ix_mco_delivery_agent",
        "ix_mco_delivery_company",
        "ix_mco_delivery_idempotency",
        "ix_mco_delivery_company_status",
    ):
        op.drop_index(name, table_name="managed_channel_outbound_deliveries")
    op.drop_table("managed_channel_outbound_deliveries")
