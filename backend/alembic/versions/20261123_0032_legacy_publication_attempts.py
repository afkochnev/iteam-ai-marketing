"""Record one logical provider attempt per legacy publication claim."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261123_0032"
down_revision: str = "20261123_0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legacy_publication_attempts",
        sa.Column("claim_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_target_id", sa.String(255), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("provider_target_id", sa.String(255), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("claim_event_id", name=op.f("pk_legacy_publication_attempts")),
        sa.ForeignKeyConstraint(
            ["claim_event_id"],
            ["publication_claim_events.id"],
            name="fk_legacy_attempt_claim",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(status = 'STARTED' AND finished_at IS NULL AND failure_code IS NULL "
            "AND external_id IS NULL AND provider_target_id IS NULL) OR "
            "(status = 'SUCCEEDED' AND finished_at IS NOT NULL AND failure_code IS NULL "
            "AND external_id IS NOT NULL AND btrim(external_id) <> '') OR "
            "(status IN ('FAILED', 'UNKNOWN') AND finished_at IS NOT NULL "
            "AND failure_code IS NOT NULL AND failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$' "
            "AND external_id IS NULL AND provider_target_id IS NULL)",
            name=op.f("ck_legacy_publication_attempts_outcome"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_legacy_publication_attempts_time_order"),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE legacy_publication_attempts IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM legacy_publication_attempts)")):
        raise RuntimeError("Refusing to drop recorded legacy publication attempts")
    op.drop_table("legacy_publication_attempts")
