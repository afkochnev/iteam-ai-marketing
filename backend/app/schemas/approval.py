from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.approval import ApprovalObjectType, ApprovalStatus


class ApprovalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=10000)


class RequiredApprovalComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str = Field(min_length=1, max_length=10000)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        if not (normalized := value.strip()):
            raise ValueError("Комментарий обязателен.")
        return normalized


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    object_type: ApprovalObjectType
    object_id: UUID
    subject_version: int
    status: ApprovalStatus
    requested_by_agent_id: UUID | None
    reviewed_by_user_id: UUID | None
    comment: str | None
    subject_snapshot: dict[str, Any]
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_at: datetime
    resolved_at: datetime | None
    updated_at: datetime
