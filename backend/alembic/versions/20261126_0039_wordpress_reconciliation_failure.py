"""Record the latest unsuccessful WordPress reconciliation separately."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261126_0039"
down_revision: str = "20261125_0038"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "studio_wordpress_operations",
        sa.Column("reconciliation_failure_code", sa.String(64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_studio_wordpress_operations_reconciliation_failure"),
        "studio_wordpress_operations",
        "reconciliation_failure_code IS NULL OR "
        "(status = 'unknown' AND reconcile_finished_at IS NOT NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE studio_wordpress_operations IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM studio_wordpress_operations "
            "WHERE reconciliation_failure_code IS NOT NULL)"
        )
    ):
        raise RuntimeError("Refusing to drop recorded WordPress reconciliation failures")
    op.drop_constraint(
        op.f("ck_studio_wordpress_operations_reconciliation_failure"),
        "studio_wordpress_operations",
        type_="check",
    )
    op.drop_column("studio_wordpress_operations", "reconciliation_failure_code")
