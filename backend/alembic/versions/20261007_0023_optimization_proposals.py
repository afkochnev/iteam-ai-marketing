"""optimization_proposals

Revision ID: 20261007_0023
Revises: 20261007_0022
Create Date: 2026-10-07 14:59:58.416037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261007_0023"
down_revision: str | None = "20261007_0022"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "campaign_optimization_proposals",
        sa.Column("campaign_id", sa.UUID(), nullable=False),
        sa.Column("feedback_analysis_id", sa.UUID(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "WAITING_APPROVAL",
                "PARTIALLY_APPROVED",
                "APPROVED",
                "REJECTED",
                "APPLIED",
                "SUPERSEDED",
                name="optimization_proposal_status",
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_by_agent_run_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_campaign_optimization_proposals_campaign_id_campaigns"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_campaign_optimization_proposals_created_by_agent_run_id_agent_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_analysis_id"],
            ["marketing_feedback_analyses.id"],
            name=op.f(
                "fk_campaign_optimization_proposals_feedback_analysis_id_marketing_feedback_analyses"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
            name=op.f("fk_campaign_optimization_proposals_reviewed_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_optimization_proposals")),
        sa.UniqueConstraint("feedback_analysis_id", name="uq_optimization_proposal_analysis"),
    )
    op.create_index(
        "ix_optimization_proposals_campaign_id",
        "campaign_optimization_proposals",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_optimization_proposals_created_at",
        "campaign_optimization_proposals",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_optimization_proposals_status",
        "campaign_optimization_proposals",
        ["status"],
        unique=False,
    )
    op.create_table(
        "campaign_optimization_actions",
        sa.Column("proposal_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "CONTENT_REVISION",
                "PUBLICATION_PLAN_REVISION",
                "STRATEGY_REVIEW",
                "EXPERIMENT",
                "NO_CHANGE",
                name="optimization_action_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "target_entity_type",
            sa.Enum(
                "CONTENT_ITEM",
                "PUBLICATION_PLAN",
                "CAMPAIGN_STRATEGY",
                "CAMPAIGN",
                name="optimization_target_entity_type",
            ),
            nullable=False,
        ),
        sa.Column("target_entity_id", sa.Uuid(), nullable=False),
        sa.Column("target_version_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_effect", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=40), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PROPOSED",
                "APPROVED",
                "REJECTED",
                "APPLIED",
                "FAILED",
                name="optimization_action_status",
            ),
            server_default="PROPOSED",
            nullable=False,
        ),
        sa.Column("source_recommendation_index", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["campaign_optimization_proposals.id"],
            name=op.f(
                "fk_campaign_optimization_actions_proposal_id_campaign_optimization_proposals"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_optimization_actions")),
        sa.UniqueConstraint("proposal_id", "position", name="uq_optimization_action_position"),
        sa.UniqueConstraint(
            "proposal_id",
            "source_recommendation_index",
            name="uq_optimization_action_recommendation",
        ),
    )
    op.create_index(
        "ix_optimization_actions_proposal_id",
        "campaign_optimization_actions",
        ["proposal_id"],
        unique=False,
    )
    op.create_index(
        "ix_optimization_actions_status", "campaign_optimization_actions", ["status"], unique=False
    )
    op.create_index(
        "ix_optimization_actions_type", "campaign_optimization_actions", ["type"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_optimization_actions_type", table_name="campaign_optimization_actions")
    op.drop_index("ix_optimization_actions_status", table_name="campaign_optimization_actions")
    op.drop_index("ix_optimization_actions_proposal_id", table_name="campaign_optimization_actions")
    op.drop_table("campaign_optimization_actions")
    op.drop_index("ix_optimization_proposals_status", table_name="campaign_optimization_proposals")
    op.drop_index(
        "ix_optimization_proposals_created_at", table_name="campaign_optimization_proposals"
    )
    op.drop_index(
        "ix_optimization_proposals_campaign_id", table_name="campaign_optimization_proposals"
    )
    op.drop_table("campaign_optimization_proposals")
    for name in (
        "optimization_action_status",
        "optimization_target_entity_type",
        "optimization_action_type",
        "optimization_proposal_status",
    ):
        op.execute(f"DROP TYPE {name}")
