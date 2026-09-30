"""add source claim provenance to publication plan items"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260930_0020"
down_revision: str | None = "20260929_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "publication_plan_items",
        sa.Column("source_claim_ids", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "publication_plan_items",
        sa.Column("source_support_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("publication_plan_items", "source_support_summary")
    op.drop_column("publication_plan_items", "source_claim_ids")
