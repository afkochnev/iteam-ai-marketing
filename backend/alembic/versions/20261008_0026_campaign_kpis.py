"""Formal campaign KPI configuration."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261008_0026"
down_revision = "20261008_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metrics = [
        "VIEWS",
        "IMPRESSIONS",
        "REACTIONS",
        "LIKES",
        "COMMENTS",
        "SHARES",
        "CLICKS",
        "SUBSCRIBERS",
        "ENGAGEMENT_RATE",
        "CTR",
    ]
    postgresql.ENUM(*metrics, name="kpi_metric").create(op.get_bind())
    postgresql.ENUM("GTE", "LTE", name="kpi_comparison").create(op.get_bind())
    op.create_table(
        "campaign_kpis",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.UUID(),
            sa.ForeignKey("campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "metric",
            postgresql.ENUM(*metrics, name="kpi_metric", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "channel",
            postgresql.ENUM("TELEGRAM", "VK", name="content_channel", create_type=False),
            nullable=True,
        ),
        sa.Column("target_value", sa.Numeric(), nullable=False),
        sa.Column(
            "comparison",
            postgresql.ENUM("GTE", "LTE", name="kpi_comparison", create_type=False),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("target_value >= 0", name="nonnegative_target"),
        sa.CheckConstraint("period_start < period_end", name="ordered_period"),
    )
    op.create_index("ix_campaign_kpis_campaign_id", "campaign_kpis", ["campaign_id"])


def downgrade() -> None:
    op.drop_table("campaign_kpis")
    postgresql.ENUM(name="kpi_comparison").drop(op.get_bind())
    postgresql.ENUM(name="kpi_metric").drop(op.get_bind())
