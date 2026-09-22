from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin


class KnowledgePackStatus(StrEnum):
    READY = "READY"
    INSUFFICIENT = "INSUFFICIENT"


class KnowledgePack(UUIDTimestampMixin, Base):
    __tablename__ = "knowledge_packs"
    __table_args__ = (
        UniqueConstraint("agent_run_id", name="uq_knowledge_packs_agent_run_id"),
        Index("ix_knowledge_packs_campaign_id", "campaign_id"),
        Index("ix_knowledge_packs_task_id", "task_id"),
        Index("ix_knowledge_packs_agent_run_id", "agent_run_id"),
        Index("ix_knowledge_packs_status", "status"),
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    agent_run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="RESTRICT"))
    created_by_agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    strategy_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[KnowledgePackStatus] = mapped_column(
        Enum(KnowledgePackStatus, name="knowledge_pack_status"), nullable=False
    )
    research_query: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    gaps: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    items: Mapped[list[KnowledgePackItem]] = relationship(
        back_populates="knowledge_pack",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="KnowledgePackItem.position",
    )


class KnowledgePackItem(UUIDTimestampMixin, Base):
    __tablename__ = "knowledge_pack_items"
    __table_args__ = (
        Index("ix_knowledge_pack_items_knowledge_pack_id", "knowledge_pack_id"),
        Index("ix_knowledge_pack_items_knowledge_item_id", "knowledge_item_id"),
        Index("ix_knowledge_pack_items_result_key", "result_key"),
    )
    knowledge_pack_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_packs.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_items.id", ondelete="RESTRICT"), nullable=False
    )
    tool_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="RESTRICT"), nullable=False
    )
    result_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="RESTRICT"), nullable=False
    )
    source_title: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    selection_reason: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    knowledge_pack: Mapped[KnowledgePack] = relationship(back_populates="items")
