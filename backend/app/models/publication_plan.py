from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin
from app.models.content import ContentChannel, ContentItem, ContentVersion


class PublicationPlanStatus(StrEnum):
    DRAFT = "DRAFT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class PublicationPlanItemStatus(StrEnum):
    PLANNED = "PLANNED"
    REMOVED = "REMOVED"


class PublicationPlan(UUIDTimestampMixin, Base):
    __tablename__ = "publication_plans"
    __table_args__ = (
        Index("ix_publication_plans_campaign_id", "campaign_id"),
        Index("ix_publication_plans_status", "status"),
    )
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PublicationPlanStatus] = mapped_column(
        Enum(PublicationPlanStatus, name="publication_plan_status"), nullable=False
    )
    planning_horizon_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    planning_horizon_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone_policy: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    generated_by_agent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT")
    )
    feedback_analysis_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("marketing_feedback_analyses.id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    items: Mapped[list[PublicationPlanItem]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="PublicationPlanItem.position"
    )


class PublicationPlanItem(UUIDTimestampMixin, Base):
    __tablename__ = "publication_plan_items"
    __table_args__ = (
        Index("ix_publication_plan_items_plan_id", "publication_plan_id"),
        Index("ix_publication_plan_items_scheduled_at", "scheduled_at"),
        Index("ix_publication_plan_items_source_version", "source_content_version_id"),
        UniqueConstraint(
            "publication_plan_id", "position", name="uq_publication_plan_item_position"
        ),
    )
    publication_plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("publication_plans.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel: Mapped[ContentChannel] = mapped_column(
        Enum(ContentChannel, name="content_channel", create_type=False), nullable=False
    )
    source_content_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT"), nullable=False
    )
    source_content_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String(500), nullable=False)
    angle: Mapped[str] = mapped_column(String(1000), nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[str] = mapped_column(String(100), nullable=False)
    message_brief: Mapped[str] = mapped_column(Text, nullable=False)
    source_claim_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    source_support_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PublicationPlanItemStatus] = mapped_column(
        Enum(PublicationPlanItemStatus, name="publication_plan_item_status"), nullable=False
    )
    plan: Mapped[PublicationPlan] = relationship(back_populates="items")
    source_content_item: Mapped[ContentItem] = relationship(lazy="selectin")
    source_content_version: Mapped[ContentVersion] = relationship(lazy="selectin")
