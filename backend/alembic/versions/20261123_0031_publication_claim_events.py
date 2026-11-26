"""Record legacy publication claims in the business transaction."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261123_0031"
down_revision: str = "20261121_0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publication_claim_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("publication_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            postgresql.ENUM("TELEGRAM", "VK", name="content_channel", create_type=False),
            nullable=False,
        ),
        sa.Column("claim_digest", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("delivery_owner", sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publication_claim_events")),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["publications.id"],
            name=op.f("fk_publication_claim_events_publication_id_publications"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["content_version_id"],
            ["content_versions.id"],
            name=op.f("fk_publication_claim_events_content_version_id_content_versions"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "publication_id", "claim_digest", name="uq_publication_claim_events_claim"
        ),
        sa.CheckConstraint(
            "claim_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_publication_claim_events_digest_format"),
        ),
        sa.CheckConstraint(
            "event_type = 'legacy.social.claimed'",
            name=op.f("ck_publication_claim_events_legacy_event"),
        ),
        sa.CheckConstraint(
            "schema_version = 1", name=op.f("ck_publication_claim_events_schema_v1")
        ),
        sa.CheckConstraint(
            "delivery_owner = 'legacy_direct'",
            name=op.f("ck_publication_claim_events_legacy_direct"),
        ),
    )
    op.create_index(
        "ix_publication_claim_events_content_version_id",
        "publication_claim_events",
        ["content_version_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE publication_claim_events IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM publication_claim_events)")):
        raise RuntimeError("Refusing to drop recorded publication claim events")
    op.drop_table("publication_claim_events")
