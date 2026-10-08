"""Human-approved descriptive marketing experiments."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261008_0025"
down_revision = "20261007_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    enums = {
        "experiment_metric": [
            "VIEWS",
            "IMPRESSIONS",
            "REACTIONS",
            "LIKES",
            "COMMENTS",
            "SHARES",
            "CLICKS",
            "SUBSCRIBERS",
        ],
        "experiment_status": ["DRAFT", "APPROVED", "RUNNING", "COMPLETED", "CANCELLED"],
        "experiment_publication_role": ["BASELINE", "EXPERIMENT"],
    }
    for name, values in enums.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind())
    metric = postgresql.ENUM(
        *enums["experiment_metric"], name="experiment_metric", create_type=False
    )
    status = postgresql.ENUM(
        *enums["experiment_status"], name="experiment_status", create_type=False
    )
    role = postgresql.ENUM(
        *enums["experiment_publication_role"], name="experiment_publication_role", create_type=False
    )
    op.add_column(
        "campaign_optimization_actions",
        sa.Column("experiment_spec", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "marketing_experiments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.UUID(),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_optimization_action_id",
            sa.UUID(),
            sa.ForeignKey("campaign_optimization_actions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("proposed_change", sa.Text(), nullable=False),
        sa.Column("success_metric", metric, nullable=False),
        sa.Column("minimum_observation_requirement", sa.Text(), nullable=True),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=False)
            for name in ["baseline_start", "baseline_end", "experiment_start", "experiment_end"]
        ],
        sa.Column("status", status, nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column(
            "limitations", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "result_data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_by_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "approved_by_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancelled_by_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_optimization_action_id", name="uq_experiment_source_action"),
        sa.CheckConstraint(
            "baseline_start < baseline_end AND baseline_end <= experiment_start "
            "AND experiment_start < experiment_end",
            name="ck_experiment_periods",
        ),
    )
    op.create_index(
        "ix_marketing_experiments_campaign_id", "marketing_experiments", ["campaign_id"]
    )
    op.create_index("ix_marketing_experiments_status", "marketing_experiments", ["status"])
    op.create_table(
        "marketing_experiment_publications",
        sa.Column(
            "experiment_id",
            sa.UUID(),
            sa.ForeignKey("marketing_experiments.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "publication_id",
            sa.UUID(),
            sa.ForeignKey("publications.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "content_version_id",
            sa.UUID(),
            sa.ForeignKey("content_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", role, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("marketing_experiment_publications")
    op.drop_table("marketing_experiments")
    op.drop_column("campaign_optimization_actions", "experiment_spec")
    for name in ["experiment_publication_role", "experiment_status", "experiment_metric"]:
        postgresql.ENUM(name=name).drop(op.get_bind())
