"""add recurring payment profiles and renewal attempts

Revision ID: f91d2e6b7c30
Revises: e71c4b9a2d60
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa


revision = "f91d2e6b7c30"
down_revision = "e71c4b9a2d60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "service_payment_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column(
            "service_subscription_id",
            sa.Integer(),
            sa.ForeignKey("service_subscriptions.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("provider_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "service_subscription_id",
            "provider",
            name="uq_service_payment_profiles_subscription_provider",
        ),
    )
    op.create_index(
        "ix_service_payment_profiles_company_status",
        "service_payment_profiles",
        ["company_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_service_payment_profiles_company_id",
        "service_payment_profiles",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_payment_profiles_service_subscription_id",
        "service_payment_profiles",
        ["service_subscription_id"],
        unique=False,
    )

    op.create_table(
        "service_renewal_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column(
            "service_subscription_id",
            sa.Integer(),
            sa.ForeignKey("service_subscriptions.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=220), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column("provider_transaction_id", sa.String(length=120), nullable=True),
        sa.Column("last_error_code", sa.String(length=160), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "provider",
            "idempotency_key",
            name="uq_service_renewal_attempt_provider_key",
        ),
    )
    op.create_index(
        "ix_service_renewal_attempt_company_status",
        "service_renewal_attempts",
        ["company_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_service_renewal_attempt_subscription_period",
        "service_renewal_attempts",
        ["service_subscription_id", "period_end"],
        unique=False,
    )
    op.create_index(
        "ix_service_renewal_attempts_company_id",
        "service_renewal_attempts",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_renewal_attempts_service_subscription_id",
        "service_renewal_attempts",
        ["service_subscription_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_renewal_attempts_period_end",
        "service_renewal_attempts",
        ["period_end"],
        unique=False,
    )
    op.create_index(
        "ix_service_renewal_attempts_status",
        "service_renewal_attempts",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_service_renewal_attempts_provider_transaction_id",
        "service_renewal_attempts",
        ["provider_transaction_id"],
        unique=False,
    )


def downgrade():
    for name in (
        "ix_service_renewal_attempts_provider_transaction_id",
        "ix_service_renewal_attempts_status",
        "ix_service_renewal_attempts_period_end",
        "ix_service_renewal_attempts_service_subscription_id",
        "ix_service_renewal_attempts_company_id",
        "ix_service_renewal_attempt_subscription_period",
        "ix_service_renewal_attempt_company_status",
    ):
        op.drop_index(name, table_name="service_renewal_attempts")
    op.drop_table("service_renewal_attempts")

    op.drop_index(
        "ix_service_payment_profiles_service_subscription_id",
        table_name="service_payment_profiles",
    )
    op.drop_index(
        "ix_service_payment_profiles_company_id",
        table_name="service_payment_profiles",
    )
    op.drop_index(
        "ix_service_payment_profiles_company_status",
        table_name="service_payment_profiles",
    )
    op.drop_table("service_payment_profiles")
