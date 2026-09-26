"""create publication domain

Revision ID: 20260925_0013
Revises: 20260923_0012
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260925_0013"
down_revision = "20260923_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    publication_status = postgresql.ENUM(
        "DRAFT",
        "WAITING_APPROVAL",
        "APPROVED",
        "SCHEDULED",
        "PUBLISHING",
        "PUBLISHED",
        "FAILED",
        "CANCELLED",
        name="publication_status",
    )
    publication_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("content_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("content_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("content_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("channel", postgresql.ENUM("TELEGRAM", "VK", name="content_channel", create_type=False), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "DRAFT",
                "WAITING_APPROVAL",
                "APPROVED",
                "SCHEDULED",
                "PUBLISHING",
                "PUBLISHED",
                "FAILED",
                "CANCELLED",
                name="publication_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_for_publish_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_for_publish_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_publications_campaign_id", "publications", ["campaign_id"])
    op.create_index("ix_publications_content_item_id", "publications", ["content_item_id"])
    op.create_index("ix_publications_status", "publications", ["status"])
    op.create_index(
        "uq_publications_active_version_channel",
        "publications",
        ["content_version_id", "channel"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('DRAFT', 'WAITING_APPROVAL', 'APPROVED', 'SCHEDULED', 'PUBLISHING')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_publications_active_version_channel", table_name="publications")
    op.drop_index("ix_publications_status", table_name="publications")
    op.drop_index("ix_publications_content_item_id", table_name="publications")
    op.drop_index("ix_publications_campaign_id", table_name="publications")
    op.drop_table("publications")
    postgresql.ENUM(name="publication_status").drop(op.get_bind(), checkfirst=True)
