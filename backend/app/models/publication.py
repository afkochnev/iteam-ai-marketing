from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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


class ReconciliationDecision(StrEnum):
    CONFIRMED_PUBLISHED = "CONFIRMED_PUBLISHED"
    CONFIRMED_NOT_PUBLISHED = "CONFIRMED_NOT_PUBLISHED"


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
        Index("ix_publications_provider_target_id", "provider_target_id"),
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
    provider_target_id: Mapped[str | None] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    execution_token: Mapped[str | None] = mapped_column(String(36), nullable=True)


class PublicationReconciliation(UUIDTimestampMixin, Base):
    __tablename__ = "publication_reconciliations"
    __table_args__ = (
        Index("ix_publication_reconciliations_publication_id", "publication_id"),
        Index("ix_publication_reconciliations_created_at", "created_at"),
    )

    publication_id: Mapped[UUID] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), nullable=False
    )
    operator_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel: Mapped[ContentChannel] = mapped_column(
        Enum(ContentChannel, name="content_channel", create_type=False), nullable=False
    )
    decision: Mapped[ReconciliationDecision] = mapped_column(
        Enum(ReconciliationDecision, name="reconciliation_decision"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    external_url: Mapped[str | None] = mapped_column(Text)
    external_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class PublicationClaimEvent(Base):
    """A committed legacy execution claim. Direct dispatch owns its delivery."""

    __tablename__ = "publication_claim_events"
    __table_args__ = (
        UniqueConstraint(
            "publication_id", "claim_digest", name="uq_publication_claim_events_claim"
        ),
        CheckConstraint("claim_digest ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("event_type = 'legacy.social.claimed'", name="legacy_event"),
        CheckConstraint("schema_version = 1", name="schema_v1"),
        CheckConstraint("delivery_owner = 'legacy_direct'", name="legacy_direct"),
        Index("ix_publication_claim_events_content_version_id", "content_version_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    publication_id: Mapped[UUID] = mapped_column(ForeignKey("publications.id", ondelete="CASCADE"))
    content_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT")
    )
    channel: Mapped[ContentChannel] = mapped_column(Enum(ContentChannel, name="content_channel"))
    claim_digest: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[int] = mapped_column(Integer)
    delivery_owner: Mapped[str] = mapped_column(String(24))


class LegacyPublicationAttempt(Base):
    """One logical provider invocation associated with a legacy publication claim."""

    __tablename__ = "legacy_publication_attempts"
    __table_args__ = (
        CheckConstraint(
            "(status = 'STARTED' AND finished_at IS NULL AND failure_code IS NULL "
            "AND external_id IS NULL AND provider_target_id IS NULL) OR "
            "(status = 'SUCCEEDED' AND finished_at IS NOT NULL AND failure_code IS NULL "
            "AND external_id IS NOT NULL AND btrim(external_id) <> '') OR "
            "(status IN ('FAILED', 'UNKNOWN') AND finished_at IS NOT NULL "
            "AND failure_code IS NOT NULL AND failure_code ~ '^[A-Z][A-Z0-9_]{0,99}$' "
            "AND external_id IS NULL AND provider_target_id IS NULL)",
            name="outcome",
        ),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="time_order"),
    )

    claim_event_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "publication_claim_events.id", name="fk_legacy_attempt_claim", ondelete="CASCADE"
        ),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(12))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_target_id: Mapped[str | None] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(255))
    provider_target_id: Mapped[str | None] = mapped_column(String(255))
    failure_code: Mapped[str | None] = mapped_column(String(100))
