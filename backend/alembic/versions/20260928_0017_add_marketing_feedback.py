"""add marketing feedback and advisory analyses

Revision ID: 20260928_0017
Revises: 20260928_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260928_0017"
down_revision: str | None = "20260928_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    feedback_source = postgresql.ENUM("HUMAN", "METRICS", "SYSTEM_ANALYSIS", name="feedback_source")
    feedback_category = postgresql.ENUM(
        "MESSAGE_RELEVANCE",
        "TONE",
        "ENGAGEMENT",
        "FORMAT",
        "CTA",
        "BUSINESS_RELEVANCE",
        "CHANNEL_FIT",
        "OTHER",
        name="feedback_category",
    )
    analysis_status = postgresql.ENUM(
        "DRAFT", "ACCEPTED", "REJECTED", "FAILED", name="feedback_analysis_status"
    )
    for enum in (feedback_source, feedback_category, analysis_status):
        enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "marketing_feedback",
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
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("publication_id", postgresql.UUID(as_uuid=True)),
        sa.Column("content_item_id", postgresql.UUID(as_uuid=True)),
        sa.Column("content_version_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "source_type",
            postgresql.ENUM(name="feedback_source", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "category", postgresql.ENUM(name="feedback_category", create_type=False), nullable=False
        ),
        sa.Column("rating", sa.Integer()),
        sa.Column("comment", sa.Text()),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["publication_id"], ["publications.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["content_item_id"], ["content_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["content_version_id"], ["content_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_marketing_feedback_campaign_id", "marketing_feedback", ["campaign_id"])
    op.create_index(
        "ix_marketing_feedback_publication_id", "marketing_feedback", ["publication_id"]
    )
    op.create_index("ix_marketing_feedback_created_at", "marketing_feedback", ["created_at"])
    op.create_table(
        "marketing_feedback_analyses",
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
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="feedback_analysis_status", create_type=False),
            nullable=False,
        ),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("recommendations", postgresql.JSONB(), nullable=False),
        sa.Column("experiment_ideas", postgresql.JSONB(), nullable=False),
        sa.Column("limitations", postgresql.JSONB(), nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feedback_analyses_campaign_id", "marketing_feedback_analyses", ["campaign_id"]
    )
    op.create_index("ix_feedback_analyses_status", "marketing_feedback_analyses", ["status"])
    op.create_index(
        "ix_feedback_analyses_created_at", "marketing_feedback_analyses", ["created_at"]
    )


def downgrade() -> None:
    for name in (
        "ix_feedback_analyses_created_at",
        "ix_feedback_analyses_status",
        "ix_feedback_analyses_campaign_id",
    ):
        op.drop_index(name, table_name="marketing_feedback_analyses")
    op.drop_table("marketing_feedback_analyses")
    for name in (
        "ix_marketing_feedback_created_at",
        "ix_marketing_feedback_publication_id",
        "ix_marketing_feedback_campaign_id",
    ):
        op.drop_index(name, table_name="marketing_feedback")
    op.drop_table("marketing_feedback")
    for name in ("feedback_analysis_status", "feedback_category", "feedback_source"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
