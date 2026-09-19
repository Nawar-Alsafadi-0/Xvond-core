"""scope payment event evidence to company checkout

Revision ID: e71c4b9a2d60
Revises: d4e8a2c7f190
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa


revision = "e71c4b9a2d60"
down_revision = "d4e8a2c7f190"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "service_payment_events",
        sa.Column("company_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "service_payment_events",
        sa.Column("service_checkout_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_payment_events_company_id",
        "service_payment_events",
        "companies",
        ["company_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_service_payment_events_checkout_id",
        "service_payment_events",
        "service_checkouts",
        ["service_checkout_id"],
        ["id"],
    )
    op.create_index(
        "ix_service_payment_events_company_id",
        "service_payment_events",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_payment_events_service_checkout_id",
        "service_payment_events",
        ["service_checkout_id"],
        unique=False,
    )
    op.create_index(
        "ix_service_payment_events_company_type",
        "service_payment_events",
        ["company_id", "event_type"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_service_payment_events_company_type",
        table_name="service_payment_events",
    )
    op.drop_index(
        "ix_service_payment_events_service_checkout_id",
        table_name="service_payment_events",
    )
    op.drop_index(
        "ix_service_payment_events_company_id",
        table_name="service_payment_events",
    )
    op.drop_constraint(
        "fk_service_payment_events_checkout_id",
        "service_payment_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_service_payment_events_company_id",
        "service_payment_events",
        type_="foreignkey",
    )
    op.drop_column("service_payment_events", "service_checkout_id")
    op.drop_column("service_payment_events", "company_id")
