from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin


class KnowledgeStoreProvider(StrEnum):
    OPENAI = "OPENAI"


class KnowledgeStoreStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    INACTIVE = "INACTIVE"


class KnowledgeSourceType(StrEnum):
    FILE_UPLOAD = "FILE_UPLOAD"
    WEBSITE = "WEBSITE"
    GOOGLE_DRIVE = "GOOGLE_DRIVE"
    YOUTUBE = "YOUTUBE"


class KnowledgeSourceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"
    INACTIVE = "INACTIVE"


class KnowledgeItemStatus(StrEnum):
    UPLOADING = "UPLOADING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class KnowledgeStore(UUIDTimestampMixin, Base):
    __tablename__ = "knowledge_stores"
    __table_args__ = (
        Index(
            "uq_knowledge_stores_active_provider",
            "provider",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )
    provider: Mapped[KnowledgeStoreProvider] = mapped_column(
        Enum(KnowledgeStoreProvider, name="knowledge_store_provider"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_store_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[KnowledgeStoreStatus] = mapped_column(
        Enum(KnowledgeStoreStatus, name="knowledge_store_status"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class KnowledgeSource(UUIDTimestampMixin, Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (Index("ix_knowledge_sources_source_type", "source_type"),)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[KnowledgeSourceType] = mapped_column(
        Enum(KnowledgeSourceType, name="knowledge_source_type"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[KnowledgeSourceStatus] = mapped_column(
        Enum(KnowledgeSourceStatus, name="knowledge_source_status"), nullable=False
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    items: Mapped[list[KnowledgeItem]] = relationship(back_populates="source", lazy="raise")


class KnowledgeItem(UUIDTimestampMixin, Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (
        Index("ix_knowledge_items_status", "status"),
        Index("ix_knowledge_items_source_id", "source_id"),
        Index("ix_knowledge_items_openai_file_id", "openai_file_id"),
        Index("ix_knowledge_items_created_at", "created_at"),
    )
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    openai_file_id: Mapped[str | None] = mapped_column(String(255))
    vector_store_file_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[KnowledgeItemStatus] = mapped_column(
        Enum(KnowledgeItemStatus, name="knowledge_item_status"), nullable=False
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[KnowledgeSource] = relationship(back_populates="items", lazy="raise")
