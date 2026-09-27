from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.content import ContentChannel
from app.models.publication import PublicationStatus


class PublicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_item_id: UUID
    content_version_id: UUID
    channel: ContentChannel


class PublicationScheduleRequest(BaseModel):
    scheduled_at: datetime = Field(description="Timezone-aware future publication time")


class PublicationProvenance(BaseModel):
    content_version_id: UUID
    source_content_version_id: UUID
    section_key: str


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
