from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.knowledge_pack import KnowledgePackStatus


class KnowledgePackItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    knowledge_item_id: UUID
    source_title: str
    filename: str | None
    file_id: str
    excerpt: str
    relevance_score: float | None
    selection_reason: str | None
    position: int
    result_key: str


class KnowledgePackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    campaign_id: UUID
    task_id: UUID
    agent_run_id: UUID
    created_by_agent_id: UUID
    strategy_version: int | None
    status: KnowledgePackStatus
    research_query: str
    summary: str
    gaps: list[str]
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_at: datetime
    items: list[KnowledgePackItemResponse]
