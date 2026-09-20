"""create agents and agent tools

Revision ID: 20260920_0002
Revises: 20260920_0001
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260920_0002"
down_revision: str | None = "20260920_0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    agent_status = postgresql.ENUM("ACTIVE", "INACTIVE", name="agent_status", create_type=False)
    agent_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agents",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("status", agent_status, nullable=False),
        sa.Column("autonomy_level", sa.Integer(), nullable=False),
        sa.Column(
            "settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "autonomy_level >= 0 AND autonomy_level <= 5",
            name=op.f("ck_agents_autonomy_level_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
    )
    op.create_index(op.f("ix_agents_slug"), "agents", ["slug"], unique=True)
    op.create_table(
        "agent_tools",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_agent_tools_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tools")),
        sa.UniqueConstraint("agent_id", "tool_name", name="uq_agent_tools_agent_id_tool_name"),
    )
    op.create_index(op.f("ix_agent_tools_agent_id"), "agent_tools", ["agent_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_tools_agent_id"), table_name="agent_tools")
    op.drop_table("agent_tools")
    op.drop_index(op.f("ix_agents_slug"), table_name="agents")
    op.drop_table("agents")
    postgresql.ENUM(name="agent_status").drop(op.get_bind(), checkfirst=True)
