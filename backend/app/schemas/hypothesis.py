from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HypothesisVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    hypothesis_id: UUID
    version_number: int
    supersedes_version_id: UUID | None
    schema_version: int
    snapshot: dict[str, Any]
    snapshot_hash: str
    created_by: UUID
    source: str
    created_at: datetime


class CampaignHypothesisItem(BaseModel):
    id: UUID
    created_at: datetime
    latest_draft_version: HypothesisVersionResponse | None = Field(
        description="Latest stored draft version, if any."
    )


class CampaignHypothesesResponse(BaseModel):
    items: list[CampaignHypothesisItem]
    total: int
    limit: int
    offset: int
    as_of: datetime
