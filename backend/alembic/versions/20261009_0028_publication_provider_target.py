"""Bind published provider ids to the exact external target."""

import sqlalchemy as sa

from alembic import op

revision = "20261009_0028"
down_revision = "20261008_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publications", sa.Column("provider_target_id", sa.String(length=255)))
    op.create_index(
        "ix_publications_provider_target_id",
        "publications",
        ["provider_target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_publications_provider_target_id", table_name="publications")
    op.drop_column("publications", "provider_target_id")
