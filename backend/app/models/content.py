from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin
from app.models.knowledge_pack import KnowledgePackItem


class ContentType(StrEnum):
    ARTICLE = "ARTICLE"
    SOCIAL_POST = "SOCIAL_POST"
    SOCIAL_POST_PACK = "SOCIAL_POST_PACK"


class ContentStatus(StrEnum):
    DRAFT = "DRAFT"
    WAITING_REVIEW = "WAITING_REVIEW"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class ContentItem(UUIDTimestampMixin, Base):
    __tablename__ = "content_items"
    __table_args__ = (
        Index("ix_content_items_campaign_id", "campaign_id"),
        Index("ix_content_items_source_task_id", "source_task_id"),
        Index("ix_content_items_content_type", "content_type"),
        Index("ix_content_items_status", "status"),
        Index("ix_content_items_created_at", "created_at"),
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False)
    source_task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False)
    content_type: Mapped[ContentType] = mapped_column(Enum(ContentType, name="content_type"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ContentStatus] = mapped_column(Enum(ContentStatus, name="content_status"), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("content_versions.id", ondelete="RESTRICT"))
    author_agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    archived_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    current_version: Mapped[ContentVersion | None] = relationship("ContentVersion", foreign_keys=[current_version_id], uselist=False, post_update=True, lazy="selectin")
    versions: Mapped[list[ContentVersion]] = relationship(back_populates="content_item", foreign_keys="ContentVersion.content_item_id", lazy="selectin", cascade="all, delete-orphan")


class ContentVersion(UUIDTimestampMixin, Base):
    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("content_item_id", "version_number", name="uq_content_versions_item_version"),
        Index("ix_content_versions_content_item_id", "content_item_id"),
        Index("ix_content_versions_source_agent_run_id", "source_agent_run_id"),
        Index("uq_content_versions_source_agent_run_id", "source_agent_run_id", unique=True, postgresql_where=text("source_agent_run_id IS NOT NULL")),
    )
    content_item_id: Mapped[UUID] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    structured_content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    source_agent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="RESTRICT"))
    change_description: Mapped[str | None] = mapped_column(Text)
    content_item: Mapped[ContentItem] = relationship(back_populates="versions", foreign_keys=[content_item_id])
    sources: Mapped[list[ContentVersionSource]] = relationship(back_populates="content_version", cascade="all, delete-orphan", lazy="selectin")


class ContentVersionSource(UUIDTimestampMixin, Base):
    __tablename__ = "content_version_sources"
    __table_args__ = (
        UniqueConstraint("content_version_id", "knowledge_pack_item_id", "section_key", name="uq_content_version_sources_ref"),
        Index("ix_content_version_sources_content_version_id", "content_version_id"),
        Index("ix_content_version_sources_knowledge_pack_item_id", "knowledge_pack_item_id"),
    )
    content_version_id: Mapped[UUID] = mapped_column(ForeignKey("content_versions.id", ondelete="CASCADE"), nullable=False)
    knowledge_pack_item_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_pack_items.id", ondelete="RESTRICT"), nullable=False)
    section_key: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content_version: Mapped[ContentVersion] = relationship(back_populates="sources")
    knowledge_pack_item: Mapped[KnowledgePackItem] = relationship(lazy="selectin")
