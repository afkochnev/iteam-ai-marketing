"""retain upload payload for resumable knowledge indexing retries"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260923_0012"
down_revision: str | Sequence[str] | None = "20260922_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_items", sa.Column("source_content", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_items", "source_content")
