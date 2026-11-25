"""Add independently versioned content assets and exact subject approvals."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261124_0035"
down_revision = "20261123_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "workspace_id = '4183ac32-0f61-54da-ad9e-ebb31696101b'", name="legacy_workspace"
        ),
        sa.CheckConstraint("kind IN ('ARTICLE', 'ILLUSTRATION')", name="kind"),
        sa.UniqueConstraint(
            "id", "workspace_id", "campaign_id", "kind", name="uq_content_assets_scope"
        ),
    )
    op.create_index(
        "ix_content_assets_campaign_order",
        "content_assets",
        ["workspace_id", "campaign_id", "created_at", "id"],
    )
    op.create_table(
        "content_asset_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), sa.ForeignKey("content_asset_versions.id")),
        sa.Column("source_version_id", sa.Uuid()),
        sa.Column("illustration_version_id", sa.Uuid()),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("asset_id", "version_number"),
        sa.UniqueConstraint(
            "id", "workspace_id", "campaign_id", name="uq_content_asset_versions_scope"
        ),
        sa.UniqueConstraint(
            "id",
            "asset_id",
            "workspace_id",
            "campaign_id",
            name="uq_content_asset_versions_asset_scope",
        ),
        sa.UniqueConstraint(
            "id",
            "workspace_id",
            "campaign_id",
            "snapshot_hash",
            name="uq_content_asset_versions_scope_hash",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "workspace_id", "campaign_id", "kind"],
            [
                "content_assets.id",
                "content_assets.workspace_id",
                "content_assets.campaign_id",
                "content_assets.kind",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["illustration_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
        ),
        sa.CheckConstraint("version_number > 0", name="version_number"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        sa.CheckConstraint(
            "illustration_version_id IS NULL OR kind = 'ARTICLE'", name="illustration_kind"
        ),
    )
    op.create_table(
        "content_asset_approvals",
        sa.Column("version_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["version_id", "workspace_id", "campaign_id", "snapshot_hash"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
                "content_asset_versions.snapshot_hash",
            ],
        ),
    )
    op.execute("""
        CREATE FUNCTION prepare_content_asset_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.version_number = 1 AND NEW.supersedes_version_id IS NOT NULL)
                OR (NEW.version_number > 1 AND NOT EXISTS (
                    SELECT 1 FROM content_asset_versions
                    WHERE id = NEW.supersedes_version_id AND asset_id = NEW.asset_id
                        AND version_number = NEW.version_number - 1
                )) THEN
                RAISE EXCEPTION 'Invalid asset predecessor' USING ERRCODE = '23514';
            END IF;
            -- Require an already recorded source, so derived links cannot form cycles.
            IF NEW.source_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM content_asset_versions
                WHERE id = NEW.source_version_id AND workspace_id = NEW.workspace_id
                    AND campaign_id = NEW.campaign_id
            ) THEN
                RAISE EXCEPTION 'Invalid asset derivation source' USING ERRCODE = '23514';
            END IF;
            IF NEW.illustration_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM content_asset_versions
                WHERE id = NEW.illustration_version_id AND kind = 'ILLUSTRATION'
                    AND workspace_id = NEW.workspace_id AND campaign_id = NEW.campaign_id
            ) THEN
                RAISE EXCEPTION 'Invalid illustration version' USING ERRCODE = '23514';
            END IF;
            NEW.snapshot_hash := encode(sha256(convert_to(jsonb_build_array(
                1, NEW.kind, NEW.payload, NEW.source_version_id, NEW.illustration_version_id
            )::text, 'UTF8')), 'hex');
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER prepare_content_asset_version BEFORE INSERT ON content_asset_versions
        FOR EACH ROW EXECUTE FUNCTION prepare_content_asset_version()
    """)
    op.execute("""
        CREATE FUNCTION require_approved_asset_illustration() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE required_version_id uuid;
        BEGIN
            SELECT illustration_version_id INTO required_version_id
            FROM content_asset_versions WHERE id = NEW.version_id;
            IF required_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM content_asset_approvals WHERE version_id = required_version_id
            ) THEN
                RAISE EXCEPTION 'The exact illustration version must be approved first'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER require_approved_asset_illustration BEFORE INSERT ON content_asset_approvals
        FOR EACH ROW EXECUTE FUNCTION require_approved_asset_illustration()
    """)
    op.execute("""
        CREATE FUNCTION reject_content_asset_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Content assets, versions and approvals are immutable'
                USING ERRCODE = '23514';
        END $$;
    """)
    for table in ("content_assets", "content_asset_versions", "content_asset_approvals"):
        op.execute(f"""
            CREATE TRIGGER reject_content_asset_mutation BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_content_asset_mutation()
        """)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("""
        LOCK TABLE content_assets, content_asset_versions, content_asset_approvals
        IN ACCESS EXCLUSIVE MODE
    """)
    )
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM content_assets)")):
        raise RuntimeError("Refusing to drop recorded content assets")
    op.drop_table("content_asset_approvals")
    op.drop_table("content_asset_versions")
    op.drop_table("content_assets")
    op.execute("DROP FUNCTION require_approved_asset_illustration()")
    op.execute("DROP FUNCTION prepare_content_asset_version()")
    op.execute("DROP FUNCTION reject_content_asset_mutation()")
