from datetime import date, datetime
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
    created_by: UUID
    creator: CampaignCreator
