from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Channel = Literal["VK", "TELEGRAM"]


class ReplanningChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_id: UUID
    scheduled_at: datetime
    channel: Channel

    @field_validator("scheduled_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Укажите часовой пояс")
        return value


class ReplanningPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[ReplanningChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_publications(self) -> "ReplanningPreviewRequest":
        ids = [change.publication_id for change in self.changes]
        if len(set(ids)) != len(ids):
            raise ValueError("Публикация указана несколько раз")
        return self


class ReplanningPreviewItem(BaseModel):
    publication_id: UUID
    content_version_id: UUID
    original_scheduled_at: datetime | None
    original_channel: Channel
    proposed_scheduled_at: datetime
    proposed_channel: Channel
    requires_content_approval: bool
    blockers: list[str]
    warnings: list[str]


class ReplanningPreviewResponse(BaseModel):
    mode: Literal["PREVIEW_ONLY"] = "PREVIEW_ONLY"
    snapshot_version: str
    generated_at: datetime
    items: list[ReplanningPreviewItem]
    message: str = (
        "Предпросмотр не изменяет расписание. Смена канала требует адаптации и нового "
        "согласования текста. Подтверждение изменений пока недоступно."
    )
