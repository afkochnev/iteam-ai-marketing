from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Date, Enum, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin
from app.models.user import User


class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class Campaign(UUIDTimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_status", "status"),
        Index("ix_campaigns_created_by", "created_by"),
        Index("ix_campaigns_created_at", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    product: Mapped[str | None] = mapped_column(Text)
    target_audience: Mapped[str | None] = mapped_column(Text)
    offer: Mapped[str | None] = mapped_column(Text)
    desired_result: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"),
        nullable=False,
        default=CampaignStatus.DRAFT,
    )
    strategy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    strategy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    creator: Mapped[User] = relationship(lazy="raise")
