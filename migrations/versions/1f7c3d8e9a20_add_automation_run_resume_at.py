"""add automation run resume deadline

Revision ID: 1f7c3d8e9a20
Revises: 0c4e91a7d2b6
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa


revision = "1f7c3d8e9a20"
down_revision = "0c4e91a7d2b6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "automation_runs",
        sa.Column("resume_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_automation_runs_status_resume_at",
        "automation_runs",
        ["status", "resume_at"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_automation_runs_status_resume_at",
        table_name="automation_runs",
    )
    op.drop_column("automation_runs", "resume_at")
