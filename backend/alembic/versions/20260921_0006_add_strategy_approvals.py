"""add strategy version and approvals

Revision ID: 20260921_0006
Revises: 20260921_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260921_0006"
down_revision: str | None = "20260921_0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    object_type = postgresql.ENUM(
        "CAMPAIGN_STRATEGY", "CONTENT_ITEM", name="approval_object_type", create_type=False
    )
    approval_status = postgresql.ENUM(
        "PENDING",
        "APPROVED",
        "REJECTED",
        "REVISION_REQUESTED",
        name="approval_status",
        create_type=False,
    )
    object_type.create(op.get_bind(), checkfirst=True)
    approval_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "campaigns", sa.Column("strategy_version", sa.Integer(), server_default="0", nullable=False)
    )
    op.create_table(
        "approvals",
        sa.Column("object_type", object_type, nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_version", sa.Integer(), nullable=False),
        sa.Column("status", approval_status, nullable=False),
        sa.Column("requested_by_agent_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("comment", sa.Text()),
        sa.Column("subject_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["requested_by_agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_approvals_object", "approvals", ["object_type", "object_id"])
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index(
        "uq_approvals_pending_subject",
        "approvals",
        ["object_type", "object_id", "subject_version"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_column("campaigns", "strategy_version")
    postgresql.ENUM(name="approval_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="approval_object_type").drop(op.get_bind(), checkfirst=True)
