"""Keep confirmed WordPress publication times on durable operations."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261128_0040"
down_revision: str = "20261126_0039"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "studio_wordpress_operations",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_studio_wordpress_operations_schedule"),
        "studio_wordpress_operations",
        "scheduled_for IS NULL OR action IN ('image','publish')",
    )
    op.execute(
        """
        CREATE FUNCTION protect_wordpress_operation_schedule() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.scheduled_for IS DISTINCT FROM OLD.scheduled_for THEN
                RAISE EXCEPTION 'WordPress operation schedule is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_wordpress_operation_schedule
        BEFORE UPDATE OF scheduled_for ON studio_wordpress_operations
        FOR EACH ROW EXECUTE FUNCTION protect_wordpress_operation_schedule()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE studio_wordpress_operations IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM studio_wordpress_operations "
            "WHERE scheduled_for IS NOT NULL)"
        )
    ):
        raise RuntimeError("Refusing to drop confirmed WordPress publication schedules")
    op.execute("DROP TRIGGER protect_wordpress_operation_schedule ON studio_wordpress_operations")
    op.execute("DROP FUNCTION protect_wordpress_operation_schedule()")
    op.drop_constraint(
        op.f("ck_studio_wordpress_operations_schedule"),
        "studio_wordpress_operations",
        type_="check",
    )
    op.drop_column("studio_wordpress_operations", "scheduled_for")
