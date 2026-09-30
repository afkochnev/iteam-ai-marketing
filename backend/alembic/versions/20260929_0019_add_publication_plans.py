"""add publication plans"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260929_0019"
down_revision: str | None = "20260928_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    plan_status = postgresql.ENUM(
        "DRAFT",
        "WAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "ARCHIVED",
        name="publication_plan_status",
    )
    item_status = postgresql.ENUM("PLANNED", "REMOVED", name="publication_plan_item_status")
    plan_status.create(op.get_bind(), checkfirst=True)
    item_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "publication_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="publication_plan_status", create_type=False),
            nullable=False,
        ),
        sa.Column("planning_horizon_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planning_horizon_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_policy", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "generated_by_agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "feedback_analysis_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("marketing_feedback_analyses.id", ondelete="RESTRICT"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
    )
    op.create_index("ix_publication_plans_campaign_id", "publication_plans", ["campaign_id"])
    op.create_index("ix_publication_plans_status", "publication_plans", ["status"])
    op.create_table(
        "publication_plan_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "publication_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("publication_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "channel", postgresql.ENUM(name="content_channel", create_type=False), nullable=False
        ),
        sa.Column(
            "source_content_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_content_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("topic", sa.String(500), nullable=False),
        sa.Column("angle", sa.String(1000), nullable=False),
        sa.Column("purpose", sa.String(500), nullable=False),
        sa.Column("format", sa.String(100), nullable=False),
        sa.Column("message_brief", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="publication_plan_item_status", create_type=False),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "publication_plan_id", "position", name="uq_publication_plan_item_position"
        ),
    )
    op.create_index(
        "ix_publication_plan_items_plan_id", "publication_plan_items", ["publication_plan_id"]
    )
    op.create_index(
        "ix_publication_plan_items_scheduled_at", "publication_plan_items", ["scheduled_at"]
    )
    op.create_index(
        "ix_publication_plan_items_source_version",
        "publication_plan_items",
        ["source_content_version_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_publication_plan_items_source_version", table_name="publication_plan_items")
    op.drop_index("ix_publication_plan_items_scheduled_at", table_name="publication_plan_items")
    op.drop_index("ix_publication_plan_items_plan_id", table_name="publication_plan_items")
    op.drop_table("publication_plan_items")
    op.drop_index("ix_publication_plans_status", table_name="publication_plans")
    op.drop_index("ix_publication_plans_campaign_id", table_name="publication_plans")
    op.drop_table("publication_plans")
    op.execute("DROP TYPE IF EXISTS publication_plan_item_status")
    op.execute("DROP TYPE IF EXISTS publication_plan_status")
