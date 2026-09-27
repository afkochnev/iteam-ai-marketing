"""create content and versions

Revision ID: 20260922_0009
Revises: 20260921_0008
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260922_0009"
down_revision = "20260921_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    content_type = postgresql.ENUM(
        "ARTICLE", "SOCIAL_POST", "SOCIAL_POST_PACK", name="content_type", create_type=False
    )
    content_status = postgresql.ENUM(
        "DRAFT",
        "WAITING_REVIEW",
        "WAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "ARCHIVED",
        name="content_status",
        create_type=False,
    )
    bind = op.get_bind()
    content_type.create(bind, checkfirst=True)
    content_status.create(bind, checkfirst=True)
    op.create_table(
        "content_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("content_type", content_type, nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", content_status, nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "author_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "content_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "content_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("structured_content", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_by_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "source_agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column("change_description", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "content_item_id", "version_number", name="uq_content_versions_item_version"
        ),
    )
    op.create_table(
        "content_version_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "content_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_pack_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_pack_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("section_key", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "content_version_id",
            "knowledge_pack_item_id",
            "section_key",
            name="uq_content_version_sources_ref",
        ),
    )
    op.create_foreign_key(
        "fk_content_items_current_version",
        "content_items",
        "content_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_content_items_campaign_id", "content_items", ["campaign_id"])
    op.create_index("ix_content_items_source_task_id", "content_items", ["source_task_id"])
    op.create_index("ix_content_items_content_type", "content_items", ["content_type"])
    op.create_index("ix_content_items_status", "content_items", ["status"])
    op.create_index("ix_content_items_created_at", "content_items", ["created_at"])
    op.create_index("ix_content_versions_content_item_id", "content_versions", ["content_item_id"])
    op.create_index(
        "ix_content_versions_source_agent_run_id", "content_versions", ["source_agent_run_id"]
    )
    op.create_index(
        "uq_content_versions_source_agent_run_id",
        "content_versions",
        ["source_agent_run_id"],
        unique=True,
        postgresql_where=sa.text("source_agent_run_id IS NOT NULL"),
    )
    op.create_index(
        "ix_content_version_sources_content_version_id",
        "content_version_sources",
        ["content_version_id"],
    )
    op.create_index(
        "ix_content_version_sources_knowledge_pack_item_id",
        "content_version_sources",
        ["knowledge_pack_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_content_version_sources_knowledge_pack_item_id", table_name="content_version_sources"
    )
    op.drop_index(
        "ix_content_version_sources_content_version_id", table_name="content_version_sources"
    )
    op.drop_table("content_version_sources")
    op.drop_constraint("fk_content_items_current_version", "content_items", type_="foreignkey")
    op.drop_index("uq_content_versions_source_agent_run_id", table_name="content_versions")
    op.drop_index("ix_content_versions_source_agent_run_id", table_name="content_versions")
    op.drop_index("ix_content_versions_content_item_id", table_name="content_versions")
    op.drop_table("content_versions")
    for name in (
        "ix_content_items_created_at",
        "ix_content_items_status",
        "ix_content_items_content_type",
        "ix_content_items_source_task_id",
        "ix_content_items_campaign_id",
    ):
        op.drop_index(name, table_name="content_items")
    op.drop_table("content_items")
    bind = op.get_bind()
    postgresql.ENUM(name="content_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="content_type").drop(bind, checkfirst=True)
