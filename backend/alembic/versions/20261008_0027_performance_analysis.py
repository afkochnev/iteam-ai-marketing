"""Durable performance analysis workflow, preserving historical analyses."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261008_0027"
down_revision = "20261008_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE task_type ADD VALUE IF NOT EXISTS 'ANALYZE_PERFORMANCE'")
    postgresql.ENUM("MANUAL", "AUTOMATIC", name="analysis_trigger_source").create(op.get_bind())
    op.add_column("marketing_feedback_analyses", sa.Column("evidence_fingerprint", sa.String(64)))
    op.add_column("marketing_feedback_analyses", sa.Column("task_id", sa.UUID()))
    op.add_column(
        "marketing_feedback_analyses",
        sa.Column(
            "trigger_source",
            postgresql.ENUM(
                "MANUAL", "AUTOMATIC", name="analysis_trigger_source", create_type=False
            ),
        ),
    )
    op.add_column(
        "marketing_feedback_analyses",
        sa.Column(
            "interpretations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_analysis_task",
        "marketing_feedback_analyses",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_analysis_campaign_evidence",
        "marketing_feedback_analyses",
        ["campaign_id", "evidence_fingerprint"],
        unique=True,
        postgresql_where=sa.text("evidence_fingerprint IS NOT NULL"),
    )


def downgrade() -> None:
    # Enum values are retained for historical tasks; removing them would destroy data.
    op.drop_index("uq_analysis_campaign_evidence", table_name="marketing_feedback_analyses")
    op.drop_constraint("fk_analysis_task", "marketing_feedback_analyses", type_="foreignkey")
    for column in ("interpretations", "trigger_source", "task_id", "evidence_fingerprint"):
        op.drop_column("marketing_feedback_analyses", column)
    postgresql.ENUM(name="analysis_trigger_source").drop(op.get_bind())
