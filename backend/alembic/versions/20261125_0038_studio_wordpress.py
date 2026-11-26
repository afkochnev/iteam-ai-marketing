"""Add durable draft, media, and publication operations for staging WordPress."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261125_0038"
down_revision: str = "20261125_0037"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_wordpress_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("article_version_id", sa.Uuid(), nullable=False),
        sa.Column("illustration_version_id", sa.Uuid(), nullable=True),
        sa.Column("target_url", sa.String(512), nullable=False),
        sa.Column("target_author_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("article_version_id", "target_url", name="uq_studio_wp_article_target"),
        sa.ForeignKeyConstraint(["article_version_id"], ["content_asset_approvals.version_id"]),
        sa.ForeignKeyConstraint(
            ["illustration_version_id"], ["content_asset_approvals.version_id"]
        ),
        sa.ForeignKeyConstraint(
            ["article_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_wp_article_scope",
        ),
        sa.ForeignKeyConstraint(
            ["illustration_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_wp_illustration_scope",
        ),
        sa.CheckConstraint("target_author_id > 0", name="target_author"),
    )
    op.create_table(
        "studio_wordpress_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("remote_id", sa.BigInteger(), nullable=True),
        sa.Column("remote_url", sa.String(2048), nullable=True),
        sa.Column("reconcile_requested_by", sa.Uuid(), nullable=True),
        sa.Column("reconcile_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["delivery_id"], ["studio_wordpress_deliveries.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reconcile_requested_by"], ["users.id"]),
        sa.UniqueConstraint("delivery_id", "action", name="uq_studio_wp_delivery_action"),
        sa.CheckConstraint("action IN ('draft','image','publish')", name="action"),
        sa.CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('unknown','failed','succeeded') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="lifecycle",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND remote_id > 0 AND remote_id IS NOT NULL "
            "AND (remote_url IS NOT NULL) = (action = 'publish')) OR "
            "(status <> 'succeeded' AND remote_id IS NULL AND remote_url IS NULL)",
            name="receipt",
        ),
    )
    op.create_index(
        "ix_studio_wp_operations_queue",
        "studio_wordpress_operations",
        ["status", "requested_at"],
    )

    op.execute(
        """
        CREATE FUNCTION protect_studio_wordpress_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'studio_wordpress_deliveries' OR TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'WordPress delivery evidence is immutable';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'queued' THEN
                    RAISE EXCEPTION 'WordPress operation must start queued';
                END IF;
                RETURN NEW;
            END IF;
            IF (NEW.id, NEW.delivery_id, NEW.action, NEW.requested_by, NEW.requested_at)
                IS DISTINCT FROM
               (OLD.id, OLD.delivery_id, OLD.action, OLD.requested_by, OLD.requested_at)
                OR NOT (
                    (OLD.status = 'queued' AND NEW.status = 'running') OR
                    (OLD.status = 'running' AND NEW.status IN ('unknown','failed','succeeded')) OR
                    (OLD.status = 'unknown' AND NEW.status IN ('unknown','succeeded'))
                )
            THEN
                RAISE EXCEPTION 'Invalid change to WordPress operation evidence';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_wordpress_delivery
        BEFORE UPDATE OR DELETE ON studio_wordpress_deliveries
        FOR EACH ROW EXECUTE FUNCTION protect_studio_wordpress_evidence()
        """
    )
    op.execute(
        """
        CREATE TRIGGER protect_wordpress_operation
        BEFORE INSERT OR UPDATE OR DELETE ON studio_wordpress_operations
        FOR EACH ROW EXECUTE FUNCTION protect_studio_wordpress_evidence()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "LOCK TABLE studio_wordpress_deliveries, studio_wordpress_operations "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM studio_wordpress_deliveries)")):
        raise RuntimeError("Refusing to drop WordPress delivery evidence")
    op.drop_table("studio_wordpress_operations")
    op.drop_table("studio_wordpress_deliveries")
    op.execute("DROP FUNCTION protect_studio_wordpress_evidence()")
