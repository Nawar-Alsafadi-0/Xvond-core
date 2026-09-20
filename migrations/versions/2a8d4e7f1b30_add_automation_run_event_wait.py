"""add automation run event wait identity

Revision ID: 2a8d4e7f1b30
Revises: 1f7c3d8e9a20
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa


revision = "2a8d4e7f1b30"
down_revision = "1f7c3d8e9a20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "automation_runs",
        sa.Column("resume_event_name", sa.String(length=120), nullable=True),
    )
    op.create_index(
        "ix_automation_runs_company_wait_event",
        "automation_runs",
        ["company_id", "status", "resume_event_name"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_automation_runs_company_wait_event",
        table_name="automation_runs",
    )
    op.drop_column("automation_runs", "resume_event_name")
