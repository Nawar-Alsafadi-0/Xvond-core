"""add company onboarding source

Revision ID: a9d3f10b6c42
Revises: f8c1a72d4e90
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "a9d3f10b6c42"
down_revision = "f8c1a72d4e90"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "companies",
        sa.Column(
            "onboarding_source",
            sa.String(length=30),
            nullable=False,
            server_default="managed",
        ),
    )
    op.create_index(
        "ix_companies_onboarding_source",
        "companies",
        ["onboarding_source"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_companies_onboarding_source", table_name="companies")
    op.drop_column("companies", "onboarding_source")
