from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin


class OptimizationProposalStatus(StrEnum):
    DRAFT = "DRAFT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"


class OptimizationActionType(StrEnum):
    CONTENT_REVISION = "CONTENT_REVISION"
    PUBLICATION_PLAN_REVISION = "PUBLICATION_PLAN_REVISION"
    STRATEGY_REVIEW = "STRATEGY_REVIEW"
    EXPERIMENT = "EXPERIMENT"
    NO_CHANGE = "NO_CHANGE"


class OptimizationActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class OptimizationTargetEntityType(StrEnum):
    CONTENT_ITEM = "CONTENT_ITEM"
    PUBLICATION_PLAN = "PUBLICATION_PLAN"
    CAMPAIGN_STRATEGY = "CAMPAIGN_STRATEGY"
    CAMPAIGN = "CAMPAIGN"


class CampaignOptimizationProposal(UUIDTimestampMixin, Base):
    __tablename__ = "campaign_optimization_proposals"
    __table_args__ = (
        UniqueConstraint("feedback_analysis_id", name="uq_optimization_proposal_analysis"),
        Index("ix_optimization_proposals_campaign_id", "campaign_id"),
        Index("ix_optimization_proposals_status", "status"),
        Index("ix_optimization_proposals_created_at", "created_at"),
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"))
    feedback_analysis_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketing_feedback_analyses.id", ondelete="RESTRICT")
    )
    strategy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[OptimizationProposalStatus] = mapped_column(
        Enum(OptimizationProposalStatus, name="optimization_proposal_status")
    )
    summary: Mapped[str] = mapped_column(Text)
    created_by_agent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT")
    )
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actions: Mapped[list[CampaignOptimizationAction]] = relationship(
        lazy="selectin", order_by="CampaignOptimizationAction.position"
    )


class CampaignOptimizationAction(UUIDTimestampMixin, Base):
    __tablename__ = "campaign_optimization_actions"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "source_recommendation_index",
            name="uq_optimization_action_recommendation",
        ),
        UniqueConstraint("proposal_id", "position", name="uq_optimization_action_position"),
        Index("ix_optimization_actions_proposal_id", "proposal_id"),
        Index("ix_optimization_actions_status", "status"),
        Index("ix_optimization_actions_type", "type"),
    )
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaign_optimization_proposals.id", ondelete="RESTRICT")
    )
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[OptimizationActionType] = mapped_column(
        Enum(OptimizationActionType, name="optimization_action_type")
    )
    target_entity_type: Mapped[OptimizationTargetEntityType] = mapped_column(
        Enum(OptimizationTargetEntityType, name="optimization_target_entity_type")
    )
    target_entity_id: Mapped[UUID]
    target_version_id: Mapped[UUID | None]
    reason: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(40))
    evidence_refs: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    status: Mapped[OptimizationActionStatus] = mapped_column(
        Enum(OptimizationActionStatus, name="optimization_action_status"),
        default=OptimizationActionStatus.PROPOSED,
        server_default="PROPOSED",
    )
    source_recommendation_index: Mapped[int] = mapped_column(Integer)
