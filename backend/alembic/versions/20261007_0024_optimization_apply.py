"""Relational provenance and uniqueness for optimization application."""

import sqlalchemy as sa

from alembic import op

revision = "20261007_0024"
down_revision = "20261007_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("optimization_action_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_optimization_action",
        "tasks",
        "campaign_optimization_actions",
        ["optimization_action_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_tasks_optimization_action", "tasks", ["optimization_action_id"])
    for column, target in [
        ("optimization_action_id", "campaign_optimization_actions"),
        ("optimization_proposal_id", "campaign_optimization_proposals"),
    ]:
        op.add_column("publication_plans", sa.Column(column, sa.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_publication_plans_" + column,
            "publication_plans",
            target,
            [column],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_unique_constraint(
        "uq_publication_plans_optimization_action", "publication_plans", ["optimization_action_id"]
    )
    op.add_column(
        "campaign_optimization_actions", sa.Column("applied_by_user_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "campaign_optimization_actions",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_optimization_action_applied_user",
        "campaign_optimization_actions",
        "users",
        ["applied_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_optimization_action_applied_user", "campaign_optimization_actions", type_="foreignkey"
    )
    op.drop_column("campaign_optimization_actions", "applied_at")
    op.drop_column("campaign_optimization_actions", "applied_by_user_id")
    op.drop_constraint(
        "uq_publication_plans_optimization_action", "publication_plans", type_="unique"
    )
    for column in ["optimization_action_id", "optimization_proposal_id"]:
        op.drop_constraint(
            "fk_publication_plans_" + column, "publication_plans", type_="foreignkey"
        )
        op.drop_column("publication_plans", column)
    op.drop_constraint("uq_tasks_optimization_action", "tasks", type_="unique")
    op.drop_constraint("fk_tasks_optimization_action", "tasks", type_="foreignkey")
    op.drop_column("tasks", "optimization_action_id")
