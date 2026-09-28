"""add durable publication reconciliation decisions

Revision ID: 20260928_0015
Revises: 20260926_0014
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260928_0015"
down_revision = "20260926_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    decision = postgresql.ENUM(
        "CONFIRMED_PUBLISHED",
        "CONFIRMED_NOT_PUBLISHED",
        name="reconciliation_decision",
    )
    decision.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "publication_reconciliations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "publication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("publications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operator_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "channel",
            postgresql.ENUM("TELEGRAM", "VK", name="content_channel", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "decision",
            postgresql.ENUM(
                "CONFIRMED_PUBLISHED",
                "CONFIRMED_NOT_PUBLISHED",
                name="reconciliation_decision",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("external_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
    )
    op.create_index(
        "ix_publication_reconciliations_publication_id",
        "publication_reconciliations",
        ["publication_id"],
    )
    op.create_index(
        "ix_publication_reconciliations_created_at",
        "publication_reconciliations",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_publication_reconciliations_created_at",
        table_name="publication_reconciliations",
    )
    op.drop_index(
        "ix_publication_reconciliations_publication_id",
        table_name="publication_reconciliations",
    )
    op.drop_table("publication_reconciliations")
    postgresql.ENUM(name="reconciliation_decision").drop(op.get_bind(), checkfirst=True)
