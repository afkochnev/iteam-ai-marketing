"""Persist exact creative generation requests and their one-shot claims."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261124_0036"
down_revision: str = "20261124_0035"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "creative_generation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("draft", postgresql.JSONB(), nullable=False),
        sa.Column("asset_kind", sa.String(24), server_default="ILLUSTRATION", nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=True),
        sa.Column("expected_current_version_id", sa.Uuid(), nullable=True),
        sa.Column("source_version_id", sa.Uuid(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("size", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_asset_id", sa.Uuid(), nullable=True),
        sa.Column("result_version_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_creative_generation_jobs")),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            name=op.f("fk_creative_generation_jobs_campaign_id_campaigns"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_creative_generation_jobs_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_creative_generation_jobs_requested_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "workspace_id", "campaign_id", "asset_kind"],
            [
                "content_assets.id",
                "content_assets.workspace_id",
                "content_assets.campaign_id",
                "content_assets.kind",
            ],
            name="fk_creative_generation_target_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["expected_current_version_id", "asset_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.asset_id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_creative_generation_expected_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_creative_generation_source_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_asset_id", "workspace_id", "campaign_id", "asset_kind"],
            [
                "content_assets.id",
                "content_assets.workspace_id",
                "content_assets.campaign_id",
                "content_assets.kind",
            ],
            name="fk_creative_generation_result_asset",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_version_id", "result_asset_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.asset_id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_creative_generation_result_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name=op.f("ck_creative_generation_jobs_status"),
        ),
        sa.CheckConstraint(
            "size IN ('1024x1024', '1536x1024', '1024x1536')",
            name=op.f("ck_creative_generation_jobs_size"),
        ),
        sa.CheckConstraint(
            "length(btrim(prompt)) BETWEEN 1 AND 16000 AND length(btrim(model)) > 0",
            name=op.f("ck_creative_generation_jobs_provider_inputs"),
        ),
        sa.CheckConstraint(
            "(asset_id IS NULL) = (expected_current_version_id IS NULL)",
            name=op.f("ck_creative_generation_jobs_exact_target"),
        ),
        sa.CheckConstraint(
            "asset_kind = 'ILLUSTRATION'", name=op.f("ck_creative_generation_jobs_asset_kind")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(draft) = 'object' AND NOT (draft ? 'source_version_id')",
            name=op.f("ck_creative_generation_jobs_draft"),
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (started_at IS NULL) "
            "AND (status <> 'PENDING' OR claim_token IS NULL) "
            "AND (status NOT IN ('RUNNING', 'SUCCEEDED', 'UNKNOWN') OR claim_token IS NOT NULL)",
            name=op.f("ck_creative_generation_jobs_claim_state"),
        ),
        sa.CheckConstraint(
            "((status = 'SUCCEEDED') = (result_version_id IS NOT NULL)) "
            "AND ((status = 'SUCCEEDED') = (result_asset_id IS NOT NULL)) "
            "AND ((status IN ('FAILED', 'UNKNOWN')) = (error_code IS NOT NULL)) "
            "AND ((status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')) = (finished_at IS NOT NULL)) "
            "AND (status <> 'SUCCEEDED' OR asset_id IS NULL OR result_asset_id = asset_id)",
            name=op.f("ck_creative_generation_jobs_result_state"),
        ),
    )
    op.create_index(
        "ix_creative_generation_jobs_pending",
        "creative_generation_jobs",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_creative_generation_jobs_running",
        "creative_generation_jobs",
        ["started_at"],
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.execute(
        """
        CREATE FUNCTION enforce_creative_generation_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'PENDING' THEN
                    RAISE EXCEPTION 'Creative generation must start pending';
                END IF;
                RETURN NEW;
            END IF;
            IF ROW(NEW.id, NEW.campaign_id, NEW.workspace_id, NEW.requested_by, NEW.draft,
                   NEW.asset_kind, NEW.asset_id, NEW.expected_current_version_id,
                   NEW.source_version_id, NEW.prompt, NEW.model, NEW.size, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.campaign_id, OLD.workspace_id, OLD.requested_by, OLD.draft,
                   OLD.asset_kind, OLD.asset_id, OLD.expected_current_version_id,
                   OLD.source_version_id, OLD.prompt, OLD.model, OLD.size, OLD.created_at) THEN
                RAISE EXCEPTION 'Creative generation authorization is immutable';
            END IF;
            IF NOT (
                (OLD.status = 'PENDING' AND NEW.status = 'RUNNING')
                OR (OLD.status = 'PENDING' AND NEW.status = 'FAILED' AND NEW.claim_token IS NULL)
                OR (OLD.status = 'RUNNING' AND NEW.status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')
                    AND ROW(NEW.claim_token, NEW.started_at)
                        IS NOT DISTINCT FROM ROW(OLD.claim_token, OLD.started_at))
            ) THEN
                RAISE EXCEPTION 'Invalid creative generation transition';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER creative_generation_transition
        BEFORE INSERT OR UPDATE ON creative_generation_jobs
        FOR EACH ROW EXECUTE FUNCTION enforce_creative_generation_transition();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("LOCK TABLE creative_generation_jobs IN ACCESS EXCLUSIVE MODE"))
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM creative_generation_jobs)")):
        raise RuntimeError("Refusing to drop recorded creative generation jobs")
    op.drop_table("creative_generation_jobs")
    op.execute("DROP FUNCTION enforce_creative_generation_transition()")
