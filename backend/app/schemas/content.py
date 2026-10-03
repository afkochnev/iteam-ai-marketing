from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.content import ContentStatus, ContentType


class ContentApprovalRequest(BaseModel):
    comment: str | None = None


class ContentRejectionRequest(BaseModel):
    comment: str


class ContentManualEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=100_000)
    expected_current_version_id: UUID
    change_description: str | None = Field(default=None, max_length=10_000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("Текст материала обязателен.")
        return normalized

    @field_validator("change_description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class ContentSourceResponse(BaseModel):
    knowledge_pack_item_id: UUID
    source_title: str
    filename: str | None
    excerpt: str
    relevance_score: float | None
    section_key: str


class ContentVersionSummary(BaseModel):
    id: UUID
    version_number: int
    change_description: str | None
    created_by_user_id: UUID | None = None
    created_by_agent_id: UUID | None = None
    source_agent_run_id: UUID | None = None
    created_at: datetime


class ContentDerivationResponse(BaseModel):
    source_content_item_id: UUID
    source_content_item_title: str
    source_content_version_id: UUID
    source_version_number: int
    section_key: str


class ContentVersionResponse(ContentVersionSummary):
    content: str
    structured_content: dict[str, object]
    sources: list[ContentSourceResponse]
    derivations: list[ContentDerivationResponse] = Field(default_factory=list)


class ContentApprovalHistory(BaseModel):
    id: UUID
    subject_version: int
    status: str
    comment: str | None
    reviewed_by_user_id: UUID | None
    created_at: datetime
    resolved_at: datetime | None


class ContentListItem(BaseModel):
    id: UUID
    campaign_id: UUID
    content_type: ContentType
    title: str
    status: ContentStatus
    current_version_number: int | None
    created_at: datetime
    updated_at: datetime
    parent_content_item_id: UUID | None = None
    channel: str | None = None
    approved_version_id: UUID | None = None
    approved_version_number: int | None = None
    current_version_id: UUID | None = None
    source_content_item_id: UUID | None = None
    source_content_item_title: str | None = None
    publication_plan_item_id: UUID | None = None
    plan_channel: str | None = None
    plan_scheduled_at: datetime | None = None


class ContentResponse(ContentListItem):
    source_task_id: UUID
    author_agent_id: UUID
    current_version: ContentVersionResponse | None
    versions: list[ContentVersionSummary]
    approval_history: list[ContentApprovalHistory] = Field(default_factory=list)
