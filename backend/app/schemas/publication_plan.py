from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.content import ContentChannel
from app.models.publication_plan import PublicationPlanItemStatus, PublicationPlanStatus


class PlanItemInput(BaseModel):
    scheduled_at: datetime
    channel: ContentChannel
    source_content_version_id: UUID
    topic: str = Field(min_length=1, max_length=500)
    angle: str = Field(min_length=1, max_length=1000)
    purpose: str = Field(min_length=1, max_length=500)
    format: str = Field(min_length=1, max_length=100)
    message_brief: str = Field(min_length=1, max_length=4000)
    source_claim_ids: list[str] = Field(default_factory=list, max_length=3)
    source_support_summary: str | None = Field(default=None, max_length=1000)

    @field_validator("scheduled_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_at must be timezone-aware")
        return value


class PublicationPlanItemUpdate(BaseModel):
    scheduled_at: datetime | None = None
    channel: ContentChannel | None = None
    source_content_version_id: UUID | None = None
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    angle: str | None = Field(default=None, min_length=1, max_length=1000)
    purpose: str | None = Field(default=None, min_length=1, max_length=500)
    format: str | None = Field(default=None, min_length=1, max_length=100)
    message_brief: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("scheduled_at")
    @classmethod
    def optional_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("scheduled_at must be timezone-aware")
        return value


class PublicationPlanReorder(BaseModel):
    item_ids: list[UUID] = Field(min_length=1, max_length=100)


class PublicationPlanCreate(BaseModel):
    planning_horizon_start: datetime
    planning_horizon_end: datetime
    timezone_policy: str = Field(default="UTC", max_length=64)
    feedback_analysis_id: UUID | None = None
    items: list[PlanItemInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_horizon(self) -> "PublicationPlanCreate":
        if self.planning_horizon_start.tzinfo is None or self.planning_horizon_end.tzinfo is None:
            raise ValueError("planning horizon must be timezone-aware")
        if self.planning_horizon_end <= self.planning_horizon_start:
            raise ValueError("planning horizon is invalid")
        if (self.planning_horizon_end - self.planning_horizon_start).days > 90:
            raise ValueError("planning horizon cannot exceed 90 days")
        return self


class PublicationPlanGenerateRequest(BaseModel):
    planning_horizon_start: datetime
    planning_horizon_end: datetime
    timezone_policy: str = Field(default="UTC", max_length=64)
    channels: list[ContentChannel] = Field(
        default_factory=lambda: list(ContentChannel), min_length=1
    )
    total_items: int = Field(default=6, ge=1, le=100)
    feedback_analysis_id: UUID | None = None

    @model_validator(mode="after")
    def validate_horizon(self) -> "PublicationPlanGenerateRequest":
        if self.planning_horizon_start.tzinfo is None or self.planning_horizon_end.tzinfo is None:
            raise ValueError("planning horizon must be timezone-aware")
        if self.planning_horizon_end <= self.planning_horizon_start:
            raise ValueError("planning horizon is invalid")
        if (self.planning_horizon_end - self.planning_horizon_start).days > 90:
            raise ValueError("planning horizon cannot exceed 90 days")
        return self


class PublicationCollisionWarning(BaseModel):
    type: str = "NEAR_EXISTING_PUBLICATION"
    publication_id: UUID
    channel: ContentChannel
    scheduled_at: datetime
    delta_minutes: int


class PublicationPlanItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    position: int
    scheduled_at: datetime
    channel: ContentChannel
    source_content_item_id: UUID
    source_content_version_id: UUID
    topic: str
    angle: str
    purpose: str
    format: str
    message_brief: str
    source_claim_ids: list[str] | None
    source_support_summary: str | None
    status: PublicationPlanItemStatus
    near_publication_warnings: list[PublicationCollisionWarning] = Field(default_factory=list)


class PublicationPlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    campaign_id: UUID
    status: PublicationPlanStatus
    planning_horizon_start: datetime
    planning_horizon_end: datetime
    timezone_policy: str
    created_by_user_id: UUID
    generated_by_agent_run_id: UUID | None
    feedback_analysis_id: UUID | None
    approved_at: datetime | None
    approved_by_user_id: UUID | None
    items: list[PublicationPlanItemResponse]


class PublicationPlanAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rationale: str = Field(min_length=1, max_length=4000)
    items: list[PlanItemInput] = Field(max_length=100)
