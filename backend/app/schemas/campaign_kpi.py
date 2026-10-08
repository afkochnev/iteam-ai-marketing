from datetime import datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.models.campaign_kpi import KPIComparison, KPIMetric
from app.models.content import ContentChannel


class KPICreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: KPIMetric
    channel: ContentChannel | None = None
    target_value: Decimal = Field(ge=0, allow_inf_nan=False)
    comparison: KPIComparison
    period_start: AwareDatetime
    period_end: AwareDatetime
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool = True

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.period_start >= self.period_end:
            raise ValueError("period_start must precede period_end")
        return self


class KPIUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: KPIMetric | None = None
    channel: ContentChannel | None = None
    target_value: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    comparison: KPIComparison | None = None
    period_start: AwareDatetime | None = None
    period_end: AwareDatetime | None = None
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None

    @model_validator(mode="after")
    def no_null_required(self) -> Self:
        for field in self.model_fields_set - {"channel", "description"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class KPIResponse(KPICreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    campaign_id: UUID
    created_at: datetime
    updated_at: datetime
