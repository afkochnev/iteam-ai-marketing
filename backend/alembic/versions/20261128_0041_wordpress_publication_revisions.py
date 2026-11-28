"""Record durable updates and restores for existing WordPress publications."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261128_0041"
down_revision: str = "20261128_0040"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_wordpress_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("expected_revision_id", sa.Uuid(), nullable=True),
        sa.Column("article_version_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("phase", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("media_id", sa.BigInteger(), nullable=True),
        sa.Column("media_slug", sa.String(200), nullable=True),
        sa.Column("reconcile_requested_by", sa.Uuid(), nullable=True),
        sa.Column("reconcile_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_failure_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["delivery_id"], ["studio_wordpress_deliveries.id"]),
        sa.ForeignKeyConstraint(["article_version_id"], ["content_asset_approvals.version_id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reconcile_requested_by"], ["users.id"]),
        sa.UniqueConstraint("id", "delivery_id", name="uq_studio_wp_revision_scope"),
        sa.ForeignKeyConstraint(
            ["expected_revision_id", "delivery_id"],
            ["studio_wordpress_revisions.id", "studio_wordpress_revisions.delivery_id"],
            name="fk_studio_wp_revision_expected",
        ),
        sa.CheckConstraint("expected_revision_id <> id", name="expected"),
        sa.CheckConstraint("mode IN ('update','restore')", name="mode"),
        sa.CheckConstraint("phase IN ('image','replace')", name="phase"),
        sa.CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('unknown','failed','succeeded') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="lifecycle",
        ),
        sa.CheckConstraint("status <> 'succeeded' OR phase = 'replace'", name="success"),
        sa.CheckConstraint(
            "(media_id IS NULL AND media_slug IS NULL) OR "
            "(phase = 'replace' AND media_id IS NOT NULL AND media_id > 0 "
            "AND media_slug IS NOT NULL AND char_length(media_slug) > 0)",
            name="media",
        ),
        sa.CheckConstraint(
            "reconciliation_failure_code IS NULL OR "
            "(status = 'unknown' AND reconcile_finished_at IS NOT NULL)",
            name="reconciliation_failure",
        ),
    )
    op.create_index(
        "uq_studio_wp_revision_active",
        "studio_wordpress_revisions",
        ["delivery_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running','unknown')"),
    )
    op.create_index(
        "uq_studio_wp_revision_successor",
        "studio_wordpress_revisions",
        ["delivery_id", "expected_revision_id"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        "ix_studio_wp_revisions_queue",
        "studio_wordpress_revisions",
        ["status", "requested_at"],
    )
    op.create_index(
        "ix_studio_wp_revisions_history",
        "studio_wordpress_revisions",
        ["delivery_id", "requested_at"],
    )

    op.execute(
        """
        CREATE FUNCTION protect_wordpress_revision_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'WordPress revision evidence is immutable';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'queued' THEN
                    RAISE EXCEPTION 'WordPress revision must start queued';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.delivery_id, NEW.expected_revision_id, NEW.article_version_id,
                NEW.mode, NEW.requested_by, NEW.requested_at) IS DISTINCT FROM
               (OLD.id, OLD.delivery_id, OLD.expected_revision_id, OLD.article_version_id,
                OLD.mode, OLD.requested_by, OLD.requested_at)
            THEN
                RAISE EXCEPTION 'WordPress revision intent is immutable';
            END IF;

            IF OLD.phase = 'image' AND OLD.status IN ('running','unknown')
                AND OLD.lease_id IS NOT NULL
                AND NEW.phase = 'replace' AND NEW.status = 'queued'
                AND NEW.media_id IS NOT NULL AND NEW.media_slug IS NOT NULL
                AND NEW.started_at IS NULL AND NEW.finished_at IS NULL
                AND NEW.lease_id IS NULL AND NEW.failure_code IS NULL
                AND NEW.reconcile_requested_by IS NULL AND NEW.reconcile_requested_at IS NULL
                AND NEW.reconcile_started_at IS NULL AND NEW.reconcile_finished_at IS NULL
                AND NEW.reconciliation_failure_code IS NULL
            THEN
                RETURN NEW;
            END IF;

            IF (NEW.phase, NEW.media_id, NEW.media_slug) IS DISTINCT FROM
               (OLD.phase, OLD.media_id, OLD.media_slug)
                OR NOT (
                    (OLD.status = 'queued' AND NEW.status = 'running') OR
                    (OLD.status = 'running' AND NEW.status IN ('unknown','failed','succeeded')) OR
                    (OLD.status = 'unknown' AND NEW.status IN ('unknown','succeeded'))
                )
            THEN
                RAISE EXCEPTION 'Invalid change to WordPress revision evidence';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_wordpress_revision
        BEFORE INSERT OR UPDATE OR DELETE ON studio_wordpress_revisions
        FOR EACH ROW EXECUTE FUNCTION protect_wordpress_revision_evidence()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE studio_wordpress_revisions IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM studio_wordpress_revisions)")):
        raise RuntimeError("Refusing to drop recorded WordPress revisions")
    op.drop_table("studio_wordpress_revisions")
    op.execute("DROP FUNCTION protect_wordpress_revision_evidence()")
