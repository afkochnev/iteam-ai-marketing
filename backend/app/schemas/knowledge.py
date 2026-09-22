from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.knowledge import (
    KnowledgeItemStatus,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
    KnowledgeStoreProvider,
    KnowledgeStoreStatus,
)


class KnowledgeStoreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    provider: KnowledgeStoreProvider
    name: str
    external_store_id: str
    status: KnowledgeStoreStatus
    is_active: bool
    created_at: datetime


class KnowledgeSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    source_type: KnowledgeSourceType
    source_url: str | None
    status: KnowledgeSourceStatus


class KnowledgeItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    source_id: UUID
    title: str
    author: str | None
    content_type: str
    original_filename: str | None
    mime_type: str | None
    file_size_bytes: int | None
    source_url: str | None
    openai_file_id: str | None
    vector_store_file_id: str | None
    status: KnowledgeItemStatus
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    error_code: str | None
    error_message: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None
    archived_at: datetime | None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=10, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def trim_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Поисковый запрос не может быть пустым.")
        return value


class KnowledgeSearchResult(BaseModel):
    result_key: str
    knowledge_item_id: UUID
    source_id: UUID
    source_title: str
    filename: str
    file_id: str
    excerpt: str
    score: float | None
    metadata: dict[str, Any]


class KnowledgeSearchResponse(BaseModel):
    query: str
    result_count: int
    results: list[KnowledgeSearchResult]
