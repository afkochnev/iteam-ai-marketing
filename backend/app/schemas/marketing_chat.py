from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.marketing_chat import MarketingMessageRole, MarketingMessageStatus

EntityType = Literal[
    "campaign", "task", "content", "publication_plan", "publication", "knowledge_pack"
]


class DirectorChatReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_type: EntityType
    entity_id: UUID
    label: str = Field(min_length=1, max_length=160)


class DirectorChatReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=6000)
    references: list[DirectorChatReference] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("message")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Ответ не может быть пустым.")
        return value

    @field_validator("limitations")
    @classmethod
    def bounded_limitations(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 1000 for item in value):
            raise ValueError("Некорректные ограничения.")
        return value


class VerifiedReference(DirectorChatReference):
    href: str


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class MessageSend(BaseModel):
    content: str = Field(min_length=1, max_length=6000)
    client_message_id: UUID

    @field_validator("content")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Введите сообщение.")
        return value


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    campaign_id: UUID
    created_by_user_id: UUID
    title: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    conversation_id: UUID
    role: MarketingMessageRole
    status: MarketingMessageStatus
    content: str
    reply_to_message_id: UUID | None
    context_snapshot_id: UUID | None
    task_id: UUID | None
    agent_run_id: UUID | None
    references: list[VerifiedReference]
    limitations: list[str]
    error_code: str | None
    error_message: str | None
    client_message_id: UUID | None
    created_at: datetime
    updated_at: datetime


class TurnResponse(BaseModel):
    user_message: MessageResponse
    assistant_message: MessageResponse
