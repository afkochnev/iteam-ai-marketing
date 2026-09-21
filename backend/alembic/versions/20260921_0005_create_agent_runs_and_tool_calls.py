"""create agent runs and tool calls

Revision ID: 20260921_0005
Revises: 20260921_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260921_0005"
down_revision: str | None = "20260921_0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    run_status = postgresql.ENUM(
        "QUEUED",
        "RUNNING",
        "WAITING_APPROVAL",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        name="agent_run_status",
        create_type=False,
    )
    tool_status = postgresql.ENUM(
        "STARTED", "COMPLETED", "FAILED", name="tool_call_status", create_type=False
    )
    run_status.create(op.get_bind(), checkfirst=True)
    tool_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "agent_runs",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", run_status, nullable=False),
        sa.Column("input_data", postgresql.JSONB(), nullable=False),
        sa.Column("output_data", postgresql.JSONB()),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("prompt_snapshot", sa.Text(), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("openai_response_id", sa.String(255)),
        sa.Column("trace_id", sa.String(255)),
        sa.Column("queue_job_id", sa.String(255)),
        sa.Column("request_count", sa.Integer()),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("total_tokens", sa.BigInteger()),
        sa.Column("estimated_cost", sa.Numeric(12, 6)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
    )
    for col in ("task_id", "agent_id", "campaign_id", "status", "created_at"):
        op.create_index(f"ix_agent_runs_{col}", "agent_runs", [col])
    op.create_index(
        "uq_agent_runs_active_task",
        "agent_runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL')"),
    )
    op.create_table(
        "tool_calls",
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("status", tool_status, nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_tool_calls_agent_run_id", "tool_calls", ["agent_run_id"])
    op.create_index("ix_tool_calls_status", "tool_calls", ["status"])


def downgrade() -> None:
    op.drop_table("tool_calls")
    op.drop_table("agent_runs")
    postgresql.ENUM(name="tool_call_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="agent_run_status").drop(op.get_bind(), checkfirst=True)
