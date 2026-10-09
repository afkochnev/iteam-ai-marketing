from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.content import ContentChannel
from app.models.publication import PublicationStatus, ReconciliationDecision
from app.models.publication_metrics import MetricsSource


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
    provider_target_id: str | None
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
    is_overdue: bool = False
    lateness_seconds: int = 0
    scheduled_at: datetime | None
    publication_plan_item_id: UUID | None = None
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
    is_overdue: bool = False
    lateness_seconds: int = 0
    scheduled_at: datetime | None
    published_at: datetime | None
    external_url: str | None
    provider_enabled: bool
    failure_code: str | None


class PublicationMetricsInput(BaseModel):
    observed_at: datetime
    views: int | None = Field(default=None, ge=0)
    impressions: int | None = Field(default=None, ge=0)
    reactions: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    clicks: int | None = Field(default=None, ge=0)
    subscribers: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=2000)


class PublicationMetricsSnapshotResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    publication_id: UUID
    channel: ContentChannel
    observed_at: datetime
    views: int | None
    impressions: int | None
    reactions: int | None
    likes: int | None
    comments: int | None
    shares: int | None
    clicks: int | None
    subscribers: int | None
    source: MetricsSource
    provider: str | None
    created_at: datetime


class PublicationMetricsResponse(BaseModel):
    publication_id: UUID
    sync_capable: bool
    latest: PublicationMetricsSnapshotResponse | None
    history: list[PublicationMetricsSnapshotResponse]


class PerformancePublicationResponse(BaseModel):
    content_title: str | None = None
    version_number: int | None = None
    publication_id: UUID
    content_item_id: UUID
    content_version_id: UUID
    channel: ContentChannel
    published_at: datetime | None
    latest_metrics_snapshot_id: UUID | None
    source: MetricsSource | None
    observed_at: datetime | None
    views: int | None
    impressions: int | None
    reactions: int | None
    likes: int | None
    comments: int | None
    shares: int | None
    clicks: int | None
    subscribers: int | None
    metrics: PublicationMetricsSnapshotResponse | None


class CampaignPerformanceResponse(BaseModel):
    total_published: int
    with_metrics: int
    metric_coverage: dict[str, float]
    totals: dict[str, int | None]
    campaign_id: UUID
    period: dict[str, datetime]
    data_quality: dict[str, int | float]
    derived: dict[str, float | None]
    kpis: list[dict[str, Any]]
    publications: list[PerformancePublicationResponse]
