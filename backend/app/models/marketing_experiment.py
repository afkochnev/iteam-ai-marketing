from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin


class ExperimentMetric(StrEnum):
    VIEWS = "VIEWS"
    IMPRESSIONS = "IMPRESSIONS"
    REACTIONS = "REACTIONS"
    LIKES = "LIKES"
    COMMENTS = "COMMENTS"
    SHARES = "SHARES"
    CLICKS = "CLICKS"
    SUBSCRIBERS = "SUBSCRIBERS"


class ExperimentStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ExperimentPublicationRole(StrEnum):
    BASELINE = "BASELINE"
    EXPERIMENT = "EXPERIMENT"


class MarketingExperiment(UUIDTimestampMixin, Base):
    __tablename__ = "marketing_experiments"
    __table_args__ = (
        UniqueConstraint("source_optimization_action_id", name="uq_experiment_source_action"),
        CheckConstraint(
            "baseline_start < baseline_end AND baseline_end <= experiment_start "
            "AND experiment_start < experiment_end",
            name="ck_experiment_periods",
        ),
        Index("ix_marketing_experiments_campaign_id", "campaign_id"),
        Index("ix_marketing_experiments_status", "status"),
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"))
    source_optimization_action_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaign_optimization_actions.id", ondelete="RESTRICT")
    )
    hypothesis: Mapped[str] = mapped_column(Text)
    proposed_change: Mapped[str] = mapped_column(Text)
    success_metric: Mapped[ExperimentMetric] = mapped_column(
        Enum(ExperimentMetric, name="experiment_metric")
    )
    minimum_observation_requirement: Mapped[str | None] = mapped_column(Text)
    baseline_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    baseline_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    experiment_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    experiment_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[ExperimentStatus] = mapped_column(
        Enum(ExperimentStatus, name="experiment_status")
    )
    result_summary: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    result_data: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketingExperimentPublication(Base):
    __tablename__ = "marketing_experiment_publications"
    experiment_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketing_experiments.id", ondelete="RESTRICT"), primary_key=True
    )
    publication_id: Mapped[UUID] = mapped_column(
        ForeignKey("publications.id", ondelete="RESTRICT"), primary_key=True
    )
    # Preserve the exact source even if a scheduled publication is later replaced.
    content_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT")
    )
    role: Mapped[ExperimentPublicationRole] = mapped_column(
        Enum(ExperimentPublicationRole, name="experiment_publication_role")
    )
