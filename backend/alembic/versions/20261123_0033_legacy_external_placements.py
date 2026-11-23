"""Record canonical, receipt-backed legacy social placements and their provider metrics."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261123_0033"
down_revision: str = "20261123_0032"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legacy_external_placements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            postgresql.ENUM("TELEGRAM", "VK", name="content_channel", create_type=False),
            nullable=False,
        ),
        sa.Column("provider_target_id", sa.String(255), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legacy_external_placements")),
        sa.ForeignKeyConstraint(
            ["claim_event_id"],
            ["legacy_publication_attempts.claim_event_id"],
            name="fk_legacy_placement_attempt",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("claim_event_id", name="uq_legacy_placement_claim"),
        sa.UniqueConstraint(
            "channel", "provider_target_id", "external_id", name="uq_legacy_placement_object"
        ),
        sa.CheckConstraint(
            "CASE WHEN external_id ~ '^[1-9][0-9]{0,18}$' "
            "THEN external_id::numeric <= 9223372036854775807 ELSE false END",
            name=op.f("ck_legacy_external_placements_external_id"),
        ),
        sa.CheckConstraint(
            "CASE WHEN provider_target_id ~ '^-?[1-9][0-9]{0,18}$' "
            "THEN provider_target_id::numeric BETWEEN -9223372036854775808 AND 9223372036854775807 "
            "ELSE false END",
            name=op.f("ck_legacy_external_placements_target_id"),
        ),
        sa.CheckConstraint(
            "channel = 'TELEGRAM' OR (channel = 'VK' AND left(provider_target_id, 1) = '-')",
            name=op.f("ck_legacy_external_placements_channel_target"),
        ),
    )
    op.add_column(
        "publication_metrics_snapshots",
        sa.Column("external_placement_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_metrics_placement",
        "publication_metrics_snapshots",
        "legacy_external_placements",
        ["external_placement_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_publication_metrics_snapshots_placement_source"),
        "publication_metrics_snapshots",
        "external_placement_id IS NULL OR source = 'PROVIDER'",
    )
    op.create_index(
        "ix_publication_metrics_placement",
        "publication_metrics_snapshots",
        ["external_placement_id"],
        postgresql_where=sa.text("external_placement_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "LOCK TABLE publication_metrics_snapshots, legacy_external_placements "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM legacy_external_placements)")):
        raise RuntimeError("Refusing to drop recorded legacy external placements")
    op.drop_index("ix_publication_metrics_placement", table_name="publication_metrics_snapshots")
    op.drop_constraint(
        op.f("ck_publication_metrics_snapshots_placement_source"),
        "publication_metrics_snapshots",
        type_="check",
    )
    op.drop_constraint("fk_metrics_placement", "publication_metrics_snapshots", type_="foreignkey")
    op.drop_column("publication_metrics_snapshots", "external_placement_id")
    op.drop_table("legacy_external_placements")
