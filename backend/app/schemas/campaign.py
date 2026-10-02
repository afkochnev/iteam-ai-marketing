from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.campaign import CampaignStatus


class CampaignInputBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=255)
    description: str | None = None
    goal: str
    product: str | None = None
    target_audience: str | None = None
    offer: str | None = None
    desired_result: str | None = None
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("name", "goal")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Поле не может быть пустым.")
        return normalized

    @field_validator("description", "product", "target_audience", "offer", "desired_result")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def validate_date_range(self) -> "CampaignInputBase":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Дата окончания не может быть раньше даты начала.")
        return self


class CampaignCreate(CampaignInputBase):
    pass


class CampaignUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    goal: str | None = None
    product: str | None = None
    target_audience: str | None = None
    offer: str | None = None
    desired_result: str | None = None
    start_date: date | None = None
    end_date: date | None = None

    @field_validator("name", "goal")
    @classmethod
    def validate_required_text(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("Поле не может быть null.")
        normalized = value.strip()
        if not normalized:
            raise ValueError("Поле не может быть пустым.")
        return normalized

    @field_validator("description", "product", "target_audience", "offer", "desired_result")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class CampaignChangeKind(StrEnum):
    ADMINISTRATIVE = "ADMINISTRATIVE"
    STRATEGIC = "STRATEGIC"


class CampaignChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: CampaignUpdate
    comment: str | None = Field(default=None, max_length=2_000)
    confirmed_impact: bool = False
    expected_updated_at: datetime | None = None

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class CampaignFieldChange(BaseModel):
    field: str
    label: str
    old_value: str | None
    new_value: str | None
    kind: CampaignChangeKind


class CampaignAffectedPublication(BaseModel):
    id: UUID
    channel: str
    scheduled_at: datetime | None


class CampaignChangeImpact(BaseModel):
    strategy_version: int
    strategy_status: str
    publication_plan_id: UUID | None
    publication_plan_status: str | None
    publication_plan_item_count: int
    future_plan_item_count: int
    approved_social_post_count: int
    scheduled_publication_count: int
    published_publication_count: int
    scheduled_publications: list[CampaignAffectedPublication]


class CampaignChangePreview(BaseModel):
    kind: CampaignChangeKind
    changes: list[CampaignFieldChange]
    impact: CampaignChangeImpact
    requires_confirmation: bool
    warning: str | None
    guarantees: list[str]


class CampaignChangeState(BaseModel):
    has_pending_strategic_changes: bool
    changed_fields: list[str]
    changed_at: datetime | None
    changed_by_user_id: UUID | None
    comment: str | None
    baseline_strategy_version: int | None
    active_strategy_version: int
    plan_requires_review: bool
    affected_article_count: int
    affected_social_post_count: int


class CampaignCreator(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    full_name: str | None
    email: str


class CampaignListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    goal: str
    product: str | None
    status: CampaignStatus
    start_date: date | None
    end_date: date | None
    created_at: datetime
    updated_at: datetime


class CampaignResponse(CampaignListItem):
    description: str | None
    target_audience: str | None
    offer: str | None
    desired_result: str | None
    strategy: dict[str, object] | None
    strategy_version: int
    created_by: UUID
    creator: CampaignCreator


class CampaignChangeApplyResponse(BaseModel):
    campaign: CampaignResponse
    preview: CampaignChangePreview


class StrategyGenerationResponse(BaseModel):
    campaign_id: UUID
    planning_task_id: UUID
    agent_run_id: UUID
    status: CampaignStatus


class StrategyApprovalResponse(BaseModel):
    campaign: CampaignResponse
    approval_id: UUID
    generated_task_ids: list[UUID]
