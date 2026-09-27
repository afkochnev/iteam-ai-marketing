from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin
from app.models.content import ContentChannel


class PublicationStatus(StrEnum):
    DRAFT = "DRAFT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_PUBLICATION_STATUSES = (
    PublicationStatus.DRAFT,
    PublicationStatus.WAITING_APPROVAL,
    PublicationStatus.APPROVED,
    PublicationStatus.SCHEDULED,
    PublicationStatus.PUBLISHING,
)

PUBLICATION_ALLOWED_TRANSITIONS: dict[PublicationStatus, frozenset[PublicationStatus]] = {
    PublicationStatus.DRAFT: frozenset({PublicationStatus.APPROVED, PublicationStatus.CANCELLED}),
    PublicationStatus.WAITING_APPROVAL: frozenset(
        {PublicationStatus.APPROVED, PublicationStatus.CANCELLED}
    ),
    PublicationStatus.APPROVED: frozenset(
        {PublicationStatus.SCHEDULED, PublicationStatus.PUBLISHING, PublicationStatus.CANCELLED}
    ),
    PublicationStatus.SCHEDULED: frozenset(
        {PublicationStatus.SCHEDULED, PublicationStatus.PUBLISHING, PublicationStatus.CANCELLED}
    ),
    PublicationStatus.PUBLISHING: frozenset(
        {PublicationStatus.PUBLISHED, PublicationStatus.FAILED}
    ),
    PublicationStatus.PUBLISHED: frozenset(),
    PublicationStatus.FAILED: frozenset(),
    PublicationStatus.CANCELLED: frozenset(),
}


class Publication(UUIDTimestampMixin, Base):
    __tablename__ = "publications"
    __table_args__ = (
        Index("ix_publications_campaign_id", "campaign_id"),
        Index("ix_publications_content_item_id", "content_item_id"),
        Index("ix_publications_status", "status"),
        Index(
            "uq_publications_active_version_channel",
            "content_version_id",
            "channel",
            unique=True,
            postgresql_where=text(
                "status IN ('DRAFT', 'WAITING_APPROVAL', 'APPROVED', 'SCHEDULED', 'PUBLISHING')"
            ),
        ),
    )

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    content_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False
    )
    content_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[ContentChannel] = mapped_column(
        Enum(ContentChannel, name="content_channel"), nullable=False
    )
    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus, name="publication_status"), nullable=False
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_for_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_for_publish_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    execution_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
