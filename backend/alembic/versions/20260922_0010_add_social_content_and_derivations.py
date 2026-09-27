"""add social content and derivations
Revision ID: 20260922_0010
Revises: 20260922_0009
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260922_0010"
down_revision = "20260922_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    channel = postgresql.ENUM("TELEGRAM", "VK", name="content_channel")
    channel.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "content_items",
        sa.Column("parent_content_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("content_items", sa.Column("channel", channel, nullable=True))
    op.create_foreign_key(
        "fk_content_items_parent",
        "content_items",
        "content_items",
        ["parent_content_item_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("content_versions", sa.Column("generation_key", sa.String(100), nullable=True))
    op.drop_index("uq_content_versions_source_agent_run_id", table_name="content_versions")
    op.create_index(
        "uq_content_versions_run_generation",
        "content_versions",
        ["source_agent_run_id", "generation_key"],
        unique=True,
        postgresql_where=sa.text("source_agent_run_id IS NOT NULL AND generation_key IS NOT NULL"),
    )
    op.create_index(
        "ix_content_items_parent_content_item_id", "content_items", ["parent_content_item_id"]
    )
    op.create_index("ix_content_items_channel", "content_items", ["channel"])
    op.create_table(
        "content_derivations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "derived_content_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_content_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_section_key", sa.String(100), nullable=False),
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
            "derived_content_version_id",
            "source_content_version_id",
            "source_section_key",
            name="uq_content_derivations_ref",
        ),
    )
    op.create_index(
        "ix_content_derivations_derived", "content_derivations", ["derived_content_version_id"]
    )
    op.create_index(
        "ix_content_derivations_source", "content_derivations", ["source_content_version_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_content_derivations_source", table_name="content_derivations")
    op.drop_index("ix_content_derivations_derived", table_name="content_derivations")
    op.drop_table("content_derivations")
    op.drop_index("ix_content_items_channel", table_name="content_items")
    op.drop_index("ix_content_items_parent_content_item_id", table_name="content_items")
    op.drop_index("uq_content_versions_run_generation", table_name="content_versions")
    op.create_index(
        "uq_content_versions_source_agent_run_id",
        "content_versions",
        ["source_agent_run_id"],
        unique=True,
        postgresql_where=sa.text("source_agent_run_id IS NOT NULL"),
    )
    op.drop_column("content_versions", "generation_key")
    op.drop_constraint("fk_content_items_parent", "content_items", type_="foreignkey")
    op.drop_column("content_items", "channel")
    op.drop_column("content_items", "parent_content_item_id")
    postgresql.ENUM(name="content_channel").drop(op.get_bind(), checkfirst=True)
