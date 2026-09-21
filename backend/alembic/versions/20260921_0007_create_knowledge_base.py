"""create knowledge base

Revision ID: 20260921_0007
Revises: 20260921_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260921_0007"
down_revision: str | None = "20260921_0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    store_provider = postgresql.ENUM("OPENAI", name="knowledge_store_provider", create_type=False)
    store_status = postgresql.ENUM(
        "ACTIVE", "ERROR", "INACTIVE", name="knowledge_store_status", create_type=False
    )
    source_type = postgresql.ENUM(
        "FILE_UPLOAD",
        "WEBSITE",
        "GOOGLE_DRIVE",
        "YOUTUBE",
        name="knowledge_source_type",
        create_type=False,
    )
    source_status = postgresql.ENUM(
        "ACTIVE", "ERROR", "INACTIVE", name="knowledge_source_status", create_type=False
    )
    item_status = postgresql.ENUM(
        "UPLOADING",
        "INDEXING",
        "READY",
        "FAILED",
        "ARCHIVED",
        name="knowledge_item_status",
        create_type=False,
    )
    for enum in (store_provider, store_status, source_type, source_status, item_status):
        enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "knowledge_stores",
        sa.Column("provider", store_provider, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("external_store_id", sa.String(255), nullable=False, unique=True),
        sa.Column("status", store_status, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
    )
    op.create_index(
        "uq_knowledge_stores_active_provider",
        "knowledge_stores",
        ["provider"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_table(
        "knowledge_sources",
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("source_type", source_type, nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("status", source_status, nullable=False),
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
    )
    op.create_index("ix_knowledge_sources_source_type", "knowledge_sources", ["source_type"])
    op.create_table(
        "knowledge_items",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("author", sa.String(255)),
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("original_filename", sa.String(255)),
        sa.Column("mime_type", sa.String(255)),
        sa.Column("file_size_bytes", sa.BigInteger()),
        sa.Column("source_url", sa.Text()),
        sa.Column("openai_file_id", sa.String(255)),
        sa.Column("vector_store_file_id", sa.String(255)),
        sa.Column("status", item_status, nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
    )
    for name, columns in (
        ("ix_knowledge_items_status", ["status"]),
        ("ix_knowledge_items_source_id", ["source_id"]),
        ("ix_knowledge_items_openai_file_id", ["openai_file_id"]),
        ("ix_knowledge_items_created_at", ["created_at"]),
    ):
        op.create_index(name, "knowledge_items", columns)


def downgrade() -> None:
    op.drop_table("knowledge_items")
    op.drop_table("knowledge_sources")
    op.drop_table("knowledge_stores")
    for name in (
        "knowledge_item_status",
        "knowledge_source_status",
        "knowledge_source_type",
        "knowledge_store_status",
        "knowledge_store_provider",
    ):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
