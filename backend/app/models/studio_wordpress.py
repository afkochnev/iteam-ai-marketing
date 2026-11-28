from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WordPressAction(StrEnum):
    DRAFT = "draft"
    IMAGE = "image"
    PUBLISH = "publish"


class WordPressState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    UNKNOWN = "unknown"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class StudioWordPressDelivery(Base):
    __tablename__ = "studio_wordpress_deliveries"
    __table_args__ = (
        UniqueConstraint("article_version_id", "target_url", name="uq_studio_wp_article_target"),
        ForeignKeyConstraint(
            ["article_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_wp_article_scope",
        ),
        ForeignKeyConstraint(
            ["illustration_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_wp_illustration_scope",
        ),
        CheckConstraint("target_author_id > 0", name="target_author"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    article_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_asset_approvals.version_id")
    )
    illustration_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_asset_approvals.version_id")
    )
    target_url: Mapped[str] = mapped_column(String(512))
    target_author_id: Mapped[int] = mapped_column(BigInteger)


class StudioWordPressOperation(Base):
    __tablename__ = "studio_wordpress_operations"
    __table_args__ = (
        UniqueConstraint("delivery_id", "action", name="uq_studio_wp_delivery_action"),
        CheckConstraint("action IN ('draft','image','publish')", name="action"),
        CheckConstraint(
            "(status = 'queued' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(status IN ('unknown','failed','succeeded') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="lifecycle",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND remote_id > 0 AND remote_id IS NOT NULL "
            "AND (remote_url IS NOT NULL) = (action = 'publish')) OR "
            "(status <> 'succeeded' AND remote_id IS NULL AND remote_url IS NULL)",
            name="receipt",
        ),
        CheckConstraint(
            "reconciliation_failure_code IS NULL OR "
            "(status = 'unknown' AND reconcile_finished_at IS NOT NULL)",
            name="reconciliation_failure",
        ),
        CheckConstraint(
            "scheduled_for IS NULL OR (action IN ('image','publish') "
            "AND (started_at IS NULL OR started_at >= scheduled_for))",
            name="schedule",
        ),
        Index("ix_studio_wp_operations_queue", "status", "requested_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(ForeignKey("studio_wordpress_deliveries.id"))
    action: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12))
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[UUID | None]
    failure_code: Mapped[str | None] = mapped_column(String(64))
    remote_id: Mapped[int | None] = mapped_column(BigInteger)
    remote_url: Mapped[str | None] = mapped_column(String(2048))
    reconcile_requested_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    reconcile_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_failure_code: Mapped[str | None] = mapped_column(String(64))
