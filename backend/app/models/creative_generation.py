from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin


class CreativeGenerationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class CreativeGenerationJob(UUIDTimestampMixin, Base):
    __tablename__ = "creative_generation_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name="status",
        ),
        CheckConstraint(
            "size IN ('1024x1024', '1536x1024', '1024x1536')",
            name="size",
        ),
        CheckConstraint(
            "length(btrim(prompt)) BETWEEN 1 AND 16000 AND length(btrim(model)) > 0",
            name="provider_inputs",
        ),
        CheckConstraint(
            "(asset_id IS NULL) = (expected_current_version_id IS NULL)",
            name="exact_target",
        ),
        CheckConstraint("asset_kind = 'ILLUSTRATION'", name="asset_kind"),
        CheckConstraint(
            "jsonb_typeof(draft) = 'object' AND NOT (draft ? 'source_version_id')", name="draft"
        ),
        CheckConstraint(
            "(claim_token IS NULL) = (started_at IS NULL) "
            "AND (status <> 'PENDING' OR claim_token IS NULL) "
            "AND (status NOT IN ('RUNNING', 'SUCCEEDED', 'UNKNOWN') OR claim_token IS NOT NULL)",
            name="claim_state",
        ),
        CheckConstraint(
            "((status = 'SUCCEEDED') = (result_version_id IS NOT NULL)) "
            "AND ((status = 'SUCCEEDED') = (result_asset_id IS NOT NULL)) "
            "AND ((status IN ('FAILED', 'UNKNOWN')) = (error_code IS NOT NULL)) "
            "AND ((status IN ('SUCCEEDED', 'FAILED', 'UNKNOWN')) = (finished_at IS NOT NULL)) "
            "AND (status <> 'SUCCEEDED' OR asset_id IS NULL OR result_asset_id = asset_id)",
            name="result_state",
        ),
        ForeignKeyConstraint(
            ["asset_id", "workspace_id", "campaign_id", "asset_kind"],
            [
                "content_assets.id",
                "content_assets.workspace_id",
                "content_assets.campaign_id",
                "content_assets.kind",
            ],
            ondelete="RESTRICT",
            name="fk_creative_generation_target_asset",
        ),
        ForeignKeyConstraint(
            ["expected_current_version_id", "asset_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.asset_id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            ondelete="RESTRICT",
            name="fk_creative_generation_expected_version",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            ondelete="RESTRICT",
            name="fk_creative_generation_source_version",
        ),
        ForeignKeyConstraint(
            ["result_asset_id", "workspace_id", "campaign_id", "asset_kind"],
            [
                "content_assets.id",
                "content_assets.workspace_id",
                "content_assets.campaign_id",
                "content_assets.kind",
            ],
            ondelete="RESTRICT",
            name="fk_creative_generation_result_asset",
        ),
        ForeignKeyConstraint(
            ["result_version_id", "result_asset_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.asset_id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            ondelete="RESTRICT",
            name="fk_creative_generation_result_version",
        ),
        Index(
            "ix_creative_generation_jobs_pending",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_creative_generation_jobs_running",
            "started_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    asset_kind: Mapped[str] = mapped_column(String(24), server_default=text("'ILLUSTRATION'"))
    asset_id: Mapped[UUID | None]
    expected_current_version_id: Mapped[UUID | None]
    source_version_id: Mapped[UUID | None]
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    size: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_token: Mapped[UUID | None]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_asset_id: Mapped[UUID | None]
    result_version_id: Mapped[UUID | None]
    error_code: Mapped[str | None] = mapped_column(String(100))
