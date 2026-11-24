from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.workspace import DEFAULT_WORKSPACE_ID


class ContentAssetKind(StrEnum):
    ARTICLE = "ARTICLE"
    ILLUSTRATION = "ILLUSTRATION"


class ContentAsset(Base):
    __tablename__ = "content_assets"
    __table_args__ = (
        # Existing campaigns are implicitly scoped to the single default workspace.
        CheckConstraint(f"workspace_id = '{DEFAULT_WORKSPACE_ID}'", name="legacy_workspace"),
        CheckConstraint("kind IN ('ARTICLE', 'ILLUSTRATION')", name="kind"),
        UniqueConstraint(
            "id", "workspace_id", "campaign_id", "kind", name="uq_content_assets_scope"
        ),
        Index(
            "ix_content_assets_campaign_order", "workspace_id", "campaign_id", "created_at", "id"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"))
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id"))
    kind: Mapped[ContentAssetKind] = mapped_column(
        Enum(ContentAssetKind, native_enum=False, length=24)
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContentAssetVersion(Base):
    __tablename__ = "content_asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version_number"),
        UniqueConstraint(
            "id", "workspace_id", "campaign_id", name="uq_content_asset_versions_scope"
        ),
        UniqueConstraint(
            "id",
            "asset_id",
            "workspace_id",
            "campaign_id",
            name="uq_content_asset_versions_asset_scope",
        ),
        UniqueConstraint(
            "id",
            "workspace_id",
            "campaign_id",
            "snapshot_hash",
            name="uq_content_asset_versions_scope_hash",
        ),
        ForeignKeyConstraint(
            ["asset_id", "workspace_id", "campaign_id", "kind"],
            [
                "content_assets.id",
                "content_assets.workspace_id",
                "content_assets.campaign_id",
                "content_assets.kind",
            ],
        ),
        ForeignKeyConstraint(
            ["source_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
        ),
        ForeignKeyConstraint(
            ["illustration_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
        ),
        CheckConstraint("version_number > 0", name="version_number"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint(
            "illustration_version_id IS NULL OR kind = 'ARTICLE'", name="illustration_kind"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    asset_id: Mapped[UUID]
    workspace_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    kind: Mapped[ContentAssetKind] = mapped_column(
        Enum(ContentAssetKind, native_enum=False, length=24)
    )
    version_number: Mapped[int]
    supersedes_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_asset_versions.id")
    )
    source_version_id: Mapped[UUID | None]
    illustration_version_id: Mapped[UUID | None]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    snapshot_hash: Mapped[str] = mapped_column(String(64), server_default=FetchedValue())
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContentAssetApproval(Base):
    __tablename__ = "content_asset_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "workspace_id", "campaign_id", "snapshot_hash"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
                "content_asset_versions.snapshot_hash",
            ],
        ),
    )

    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
