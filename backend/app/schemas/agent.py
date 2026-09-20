from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.agent import AgentRole, AgentSlug, AgentStatus


class AgentToolResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tool_name: str
    is_enabled: bool
    requires_approval: bool
    settings: dict[str, Any]


class AgentListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: AgentSlug
    role: AgentRole
    description: str | None
    model: str | None
    status: AgentStatus
    autonomy_level: int


class AgentResponse(AgentListItem):
    system_prompt: str
    settings: dict[str, Any]
    tools: list[AgentToolResponse]
    created_at: datetime
    updated_at: datetime


class AgentUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=20_000)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    model: str | None = Field(default=None, max_length=255)
    status: AgentStatus | None = None
    autonomy_level: int | None = Field(default=None, ge=0, le=5)

    @field_validator("system_prompt")
    @classmethod
    def validate_prompt(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("System prompt не может быть пустым.")
        return value.strip() if value is not None else None

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class AgentToolUpdate(BaseModel):
    is_enabled: bool | None = None
    requires_approval: bool | None = None
