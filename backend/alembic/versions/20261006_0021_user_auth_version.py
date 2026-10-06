"""Add backend-owned user session version."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261006_0021"
down_revision: str | None = "20260930_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("users", "auth_version")
