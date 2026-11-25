"""Persist the test CMS draft and its independently approved publication."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261125_0037"
down_revision: str = "20261124_0036"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_website_pages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("article_version_id", sa.Uuid(), nullable=False),
        sa.Column("illustration_version_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"]),
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
            name="fk_studio_website_article_scope",
        ),
        sa.ForeignKeyConstraint(
            ["illustration_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_website_illustration_scope",
        ),
        sa.UniqueConstraint("article_version_id"),
        sa.CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
        sa.CheckConstraint(
            "(published_at IS NULL) = (published_by IS NULL)", name="publish_evidence"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION enforce_studio_website_page() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Website test evidence cannot be deleted';
            ELSIF TG_OP = 'INSERT' THEN
                IF NEW.published_at IS NOT NULL OR NOT EXISTS (
                    SELECT 1 FROM content_asset_versions AS article
                    WHERE article.id = NEW.article_version_id AND article.kind = 'ARTICLE'
                      AND NEW.snapshot->>'article_snapshot_hash' = article.snapshot_hash
                      AND NEW.snapshot->'article' = article.payload
                      AND article.illustration_version_id
                          IS NOT DISTINCT FROM NEW.illustration_version_id
                ) THEN
                    RAISE EXCEPTION 'Create a website draft with the exact linked illustration';
                END IF;
            ELSIF OLD.published_at IS NOT NULL OR NEW.published_at IS NULL OR
                (to_jsonb(NEW) - 'published_at' - 'published_by') IS DISTINCT FROM
                (to_jsonb(OLD) - 'published_at' - 'published_by') THEN
                RAISE EXCEPTION 'Only the first website publication can alter its draft';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER studio_website_page_transition
        BEFORE INSERT OR UPDATE OR DELETE ON studio_website_pages
        FOR EACH ROW EXECUTE FUNCTION enforce_studio_website_page()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE studio_website_pages IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM studio_website_pages)")):
        raise RuntimeError("Refusing to drop recorded website test evidence")
    op.drop_table("studio_website_pages")
    op.execute("DROP FUNCTION enforce_studio_website_page()")
