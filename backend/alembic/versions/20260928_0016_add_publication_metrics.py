"""add append-only publication metrics snapshots

Revision ID: 20260928_0016
Revises: 20260928_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260928_0016"
down_revision: str | None = "20260928_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metrics_source = postgresql.ENUM("PROVIDER", "MANUAL", name="metrics_source")
    metrics_source.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "publication_metrics_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("publication_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            postgresql.ENUM("TELEGRAM", "VK", name="content_channel", create_type=False),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("impressions", sa.Integer(), nullable=True),
        sa.Column("reactions", sa.Integer(), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("subscribers", sa.Integer(), nullable=True),
        sa.Column(
            "source",
            postgresql.ENUM("PROVIDER", "MANUAL", name="metrics_source", create_type=False),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_publication_metrics_publication_id", "publication_metrics_snapshots", ["publication_id"]
    )
    op.create_index(
        "ix_publication_metrics_observed_at", "publication_metrics_snapshots", ["observed_at"]
    )
    op.create_index(
        "ix_publication_metrics_publication_observed",
        "publication_metrics_snapshots",
        ["publication_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_publication_metrics_publication_observed", table_name="publication_metrics_snapshots"
    )
    op.drop_index("ix_publication_metrics_observed_at", table_name="publication_metrics_snapshots")
    op.drop_index(
        "ix_publication_metrics_publication_id", table_name="publication_metrics_snapshots"
    )
    op.drop_table("publication_metrics_snapshots")
    sa.Enum(name="metrics_source").drop(op.get_bind(), checkfirst=True)
