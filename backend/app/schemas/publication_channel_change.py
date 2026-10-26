from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.content import ContentChannel


class ChannelChangePrepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_content_version_id: UUID
    channel: ContentChannel
    content: str = Field(min_length=1, max_length=100000)

    @field_validator("content")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Укажите адаптированный текст")
        return value.strip()


class ChannelChangeConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_item_id: UUID
    expected_content_version_id: UUID
    scheduled_at: datetime
    confirmed: Literal[True]

    @field_validator("scheduled_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Укажите часовой пояс")
        return value


class ChannelChangePrepared(BaseModel):
    content_item_id: UUID
    content_version_id: UUID
    channel: ContentChannel
    message: str = "Текст подготовлен для отдельного согласования. Исходная отправка не изменена."
