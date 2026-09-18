"""add durable automation event outbox

Revision ID: 0c4e91a7d2b6
Revises: f91d2e6b7c30
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0c4e91a7d2b6"
down_revision = "f91d2e6b7c30"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "automation_event_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id"),
            nullable=False,
        ),
        sa.Column("event_name", sa.String(length=120), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("source_id", sa.String(length=120), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "company_id",
            "event_id",
            name="uq_automation_event_outbox_company_event",
        ),
    )
    op.create_index(
        "ix_automation_event_outbox_company_id",
        "automation_event_outbox",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_automation_event_outbox_event_name",
        "automation_event_outbox",
        ["event_name"],
        unique=False,
    )
    op.create_index(
        "ix_automation_event_outbox_status_available",
        "automation_event_outbox",
        ["status", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_automation_event_outbox_company_created",
        "automation_event_outbox",
        ["company_id", "created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_automation_event_outbox_company_created",
        table_name="automation_event_outbox",
    )
    op.drop_index(
        "ix_automation_event_outbox_status_available",
        table_name="automation_event_outbox",
    )
    op.drop_index(
        "ix_automation_event_outbox_event_name",
        table_name="automation_event_outbox",
    )
    op.drop_index(
        "ix_automation_event_outbox_company_id",
        table_name="automation_event_outbox",
    )
    op.drop_table("automation_event_outbox")
