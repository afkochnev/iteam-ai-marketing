"""create tasks and dependencies

Revision ID: 20260921_0004
Revises: 20260921_0003
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260921_0004"
down_revision: str | None = "20260921_0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    task_status = postgresql.ENUM(
        "NEW",
        "BLOCKED",
        "READY",
        "IN_PROGRESS",
        "WAITING_REVIEW",
        "WAITING_APPROVAL",
        "APPROVED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        name="task_status",
        create_type=False,
    )
    task_priority = postgresql.ENUM(
        "LOW", "NORMAL", "HIGH", "URGENT", name="task_priority", create_type=False
    )
    task_type = postgresql.ENUM(
        "CAMPAIGN_PLANNING",
        "KNOWLEDGE_RESEARCH",
        "WRITE_ARTICLE",
        "CREATE_SOCIAL_POSTS",
        "CONTENT_REVISION",
        "MANUAL",
        name="task_type",
        create_type=False,
    )
    for enum in (task_status, task_priority, task_type):
        enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "tasks",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_type", task_type, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("assigned_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("priority", task_priority, nullable=False),
        sa.Column("status", task_status, nullable=False),
        sa.Column(
            "input_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "output_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["assigned_agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("campaign_id", "assigned_agent_id", "status", "task_type", "created_at"):
        op.create_index(f"ix_tasks_{column}", "tasks", [column])
    op.create_table(
        "task_dependencies",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("depends_on_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("task_id <> depends_on_task_id", name="task_dependency_not_self"),
        sa.ForeignKeyConstraint(["depends_on_task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependencies_pair"),
    )
    op.create_index("ix_task_dependencies_task_id", "task_dependencies", ["task_id"])
    op.create_index(
        "ix_task_dependencies_depends_on_task_id", "task_dependencies", ["depends_on_task_id"]
    )


def downgrade() -> None:
    op.drop_table("task_dependencies")
    op.drop_table("tasks")
    for name in ("task_type", "task_priority", "task_status"):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
