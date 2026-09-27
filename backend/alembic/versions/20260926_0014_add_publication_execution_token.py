"""add publication execution ownership token

Revision ID: 20260926_0014
Revises: 20260925_0013
"""

import sqlalchemy as sa

from alembic import op

revision = "20260926_0014"
down_revision = "20260925_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publications", sa.Column("execution_token", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("publications", "execution_token")
