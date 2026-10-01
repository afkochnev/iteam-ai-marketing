from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.content import ContentStatus, ContentType


class ContentApprovalRequest(BaseModel):
    comment: str | None = None


class ContentRejectionRequest(BaseModel):
    comment: str


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
    created_at: datetime


class ContentVersionResponse(ContentVersionSummary):
    content: str
    structured_content: dict[str, object]
    sources: list[ContentSourceResponse]


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
    current_version_id: UUID | None = None
    publication_plan_item_id: UUID | None = None
    plan_channel: str | None = None
    plan_scheduled_at: datetime | None = None


class ContentResponse(ContentListItem):
    source_task_id: UUID
    author_agent_id: UUID
    current_version: ContentVersionResponse | None
    versions: list[ContentVersionSummary]
    approval_history: list[ContentApprovalHistory] = []
