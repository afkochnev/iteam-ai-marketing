from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StudioWebsitePage(Base):
    __tablename__ = "studio_website_pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["article_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_website_article_scope",
        ),
        ForeignKeyConstraint(
            ["illustration_version_id", "workspace_id", "campaign_id"],
            [
                "content_asset_versions.id",
                "content_asset_versions.workspace_id",
                "content_asset_versions.campaign_id",
            ],
            name="fk_studio_website_illustration_scope",
        ),
        CheckConstraint("jsonb_typeof(snapshot) = 'object'", name="snapshot_object"),
        CheckConstraint("(published_at IS NULL) = (published_by IS NULL)", name="publish_evidence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    article_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_asset_approvals.version_id"), unique=True
    )
    illustration_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_asset_approvals.version_id")
    )
    workspace_id: Mapped[UUID]
    campaign_id: Mapped[UUID]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
