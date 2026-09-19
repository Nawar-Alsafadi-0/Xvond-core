"""add service checkout and payment event tracking

Revision ID: b91f2d6a4e70
Revises: a9d3f10b6c42
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa


revision = "b91f2d6a4e70"
down_revision = "a9d3f10b6c42"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_checkouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column(
            "service_subscription_id",
            sa.Integer(),
            sa.ForeignKey("service_subscriptions.id"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("service_plans.id"), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_transaction_id", sa.String(length=120), nullable=False),
        sa.Column("provider_subscription_id", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("checkout_url", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "provider_transaction_id",
            name="uq_service_checkouts_provider_transaction",
        ),
    )
    op.create_index(
        "ix_service_checkouts_company_status",
        "service_checkouts",
        ["company_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_service_checkouts_subscription_plan",
        "service_checkouts",
        ["service_subscription_id", "plan_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_checkouts_company_id",
        "service_checkouts",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_checkouts_service_subscription_id",
        "service_checkouts",
        ["service_subscription_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_checkouts_plan_id",
        "service_checkouts",
        ["plan_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_checkouts_provider_subscription_id",
        "service_checkouts",
        ["provider_subscription_id"],
        unique=False,
    )

    op.create_table(
        "service_payment_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_event_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "provider_event_id",
            name="uq_service_payment_events_provider_event",
        ),
    )
    op.create_index(
        "ix_service_payment_events_provider_created",
        "service_payment_events",
        ["provider", "created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_service_payment_events_provider_created",
        table_name="service_payment_events",
    )
    op.drop_table("service_payment_events")

    op.drop_index(
        "ix_service_checkouts_provider_subscription_id",
        table_name="service_checkouts",
    )
    op.drop_index("ix_service_checkouts_plan_id", table_name="service_checkouts")
    op.drop_index(
        "ix_service_checkouts_service_subscription_id",
        table_name="service_checkouts",
    )
    op.drop_index("ix_service_checkouts_company_id", table_name="service_checkouts")
    op.drop_index(
        "ix_service_checkouts_subscription_plan",
        table_name="service_checkouts",
    )
    op.drop_index(
        "ix_service_checkouts_company_status",
        table_name="service_checkouts",
    )
    op.drop_table("service_checkouts")
