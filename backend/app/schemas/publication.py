from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.content import ContentChannel
from app.models.publication import PublicationStatus, ReconciliationDecision


class PublicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_item_id: UUID
    content_version_id: UUID
    channel: ContentChannel


class PublicationScheduleRequest(BaseModel):
    scheduled_at: datetime = Field(description="Timezone-aware future publication time")


class PublicationReconcilePublishedRequest(BaseModel):
    external_id: str = Field(min_length=1, max_length=255)
    external_url: str | None = None
    published_at: datetime | None = None
    note: str | None = Field(default=None, max_length=2000)


class PublicationReconcileNotPublishedRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class PublicationProvenance(BaseModel):
    content_version_id: UUID
    source_content_version_id: UUID
    section_key: str


class PublicationReconciliationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    publication_id: UUID
    operator_user_id: UUID
    channel: ContentChannel
    decision: ReconciliationDecision
    external_id: str | None
    external_url: str | None
    external_published_at: datetime | None
    note: str | None
    created_at: datetime


class PublicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    content_item_id: UUID
    content_version_id: UUID
    channel: ContentChannel
    status: PublicationStatus
    scheduled_at: datetime | None
    approved_for_publish_at: datetime | None
    approved_for_publish_by: UUID | None
    external_id: str | None
    external_url: str | None
    published_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    provider_enabled: bool = True
    provenance: list[PublicationProvenance]
    retry_allowed: bool = False
    reconciliation_required: bool = False
    reconciliation_history: list[PublicationReconciliationResponse] = Field(default_factory=list)


class PublicationCalendarItem(BaseModel):
    """Safe, compact read model used by the campaign calendar."""

    publication_id: UUID
    content_item_id: UUID
    content_version_id: UUID
    title: str
    channel: ContentChannel
    status: PublicationStatus
    scheduled_at: datetime | None
    published_at: datetime | None
    external_url: str | None
    provider_enabled: bool
    failure_code: str | None
