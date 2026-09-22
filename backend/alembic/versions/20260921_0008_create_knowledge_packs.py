"""create knowledge packs

Revision ID: 20260921_0008
Revises: 20260921_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260921_0008"
down_revision: str | None = "20260921_0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pack_status = postgresql.ENUM(
        "READY", "INSUFFICIENT", name="knowledge_pack_status", create_type=False
    )
    pack_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "knowledge_packs",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_version", sa.Integer()),
        sa.Column("status", pack_status, nullable=False),
        sa.Column("research_query", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "gaps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("agent_run_id", name="uq_knowledge_packs_agent_run_id"),
    )
    for name, columns in (
        ("ix_knowledge_packs_campaign_id", ["campaign_id"]),
        ("ix_knowledge_packs_task_id", ["task_id"]),
        ("ix_knowledge_packs_agent_run_id", ["agent_run_id"]),
        ("ix_knowledge_packs_status", ["status"]),
    ):
        op.create_index(name, "knowledge_packs", columns)
    op.create_table(
        "knowledge_pack_items",
        sa.Column("knowledge_pack_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool_call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_key", sa.String(64), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_title", sa.String(255), nullable=False),
        sa.Column("filename", sa.String(255)),
        sa.Column("file_id", sa.String(255), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("relevance_score", sa.Float()),
        sa.Column("selection_reason", sa.Text()),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["knowledge_pack_id"], ["knowledge_packs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_item_id"], ["knowledge_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tool_call_id"], ["tool_calls.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="RESTRICT"),
    )
    for name, columns in (
        ("ix_knowledge_pack_items_knowledge_pack_id", ["knowledge_pack_id"]),
        ("ix_knowledge_pack_items_knowledge_item_id", ["knowledge_item_id"]),
        ("ix_knowledge_pack_items_result_key", ["result_key"]),
    ):
        op.create_index(name, "knowledge_pack_items", columns)


def downgrade() -> None:
    op.drop_table("knowledge_pack_items")
    op.drop_table("knowledge_packs")
    postgresql.ENUM(name="knowledge_pack_status").drop(op.get_bind(), checkfirst=True)
