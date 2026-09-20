"""add employee-owned file assets

Revision ID: a13f9c2e7b40
Revises: 2a8d4e7f1b30
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "a13f9c2e7b40"
down_revision = "2a8d4e7f1b30"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "employee_file_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("ai_agents.id"), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_employee_file_assets_company_id",
        "employee_file_assets",
        ["company_id"],
        unique=False,
    )
    op.create_index(
        "ix_employee_file_assets_agent_id",
        "employee_file_assets",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_employee_file_assets_sha256",
        "employee_file_assets",
        ["sha256"],
        unique=False,
    )
    op.create_index(
        "ix_employee_file_assets_company_agent_enabled",
        "employee_file_assets",
        ["company_id", "agent_id", "enabled"],
        unique=False,
    )


def downgrade():
    for name in (
        "ix_employee_file_assets_company_agent_enabled",
        "ix_employee_file_assets_sha256",
        "ix_employee_file_assets_agent_id",
        "ix_employee_file_assets_company_id",
    ):
        op.drop_index(name, table_name="employee_file_assets")
    op.drop_table("employee_file_assets")
