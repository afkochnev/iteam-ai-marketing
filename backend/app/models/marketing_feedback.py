from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin


class FeedbackSource(StrEnum):
    HUMAN = "HUMAN"
    METRICS = "METRICS"
    SYSTEM_ANALYSIS = "SYSTEM_ANALYSIS"


class FeedbackCategory(StrEnum):
    MESSAGE_RELEVANCE = "MESSAGE_RELEVANCE"
    TONE = "TONE"
    ENGAGEMENT = "ENGAGEMENT"
    FORMAT = "FORMAT"
    CTA = "CTA"
    BUSINESS_RELEVANCE = "BUSINESS_RELEVANCE"
    CHANNEL_FIT = "CHANNEL_FIT"
    OTHER = "OTHER"


class FeedbackAnalysisStatus(StrEnum):
    DRAFT = "DRAFT"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class MarketingFeedback(UUIDTimestampMixin, Base):
    __tablename__ = "marketing_feedback"
    __table_args__ = (
        Index("ix_marketing_feedback_campaign_id", "campaign_id"),
        Index("ix_marketing_feedback_publication_id", "publication_id"),
        Index("ix_marketing_feedback_created_at", "created_at"),
    )

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    publication_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("publications.id", ondelete="RESTRICT")
    )
    content_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT")
    )
    content_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT")
    )
    source_type: Mapped[FeedbackSource] = mapped_column(
        Enum(FeedbackSource, name="feedback_source"), nullable=False
    )
    category: Mapped[FeedbackCategory] = mapped_column(
        Enum(FeedbackCategory, name="feedback_category"), nullable=False
    )
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))


class MarketingFeedbackAnalysis(UUIDTimestampMixin, Base):
    __tablename__ = "marketing_feedback_analyses"
    __table_args__ = (
        Index("ix_feedback_analyses_campaign_id", "campaign_id"),
        Index("ix_feedback_analyses_status", "status"),
        Index("ix_feedback_analyses_created_at", "created_at"),
    )

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[FeedbackAnalysisStatus] = mapped_column(
        Enum(FeedbackAnalysisStatus, name="feedback_analysis_status"), nullable=False
    )
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    recommendations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    experiment_ideas: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    agent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    generated_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True))
