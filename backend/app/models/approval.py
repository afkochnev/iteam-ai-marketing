from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin
from app.models.agent import Agent
from app.models.user import User


class ApprovalObjectType(StrEnum):
    CAMPAIGN_STRATEGY = "CAMPAIGN_STRATEGY"
    CONTENT_ITEM = "CONTENT_ITEM"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVISION_REQUESTED = "REVISION_REQUESTED"


class Approval(UUIDTimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_object", "object_type", "object_id"),
        Index("ix_approvals_status", "status"),
        Index(
            "uq_approvals_pending_subject",
            "object_type",
            "object_id",
            "subject_version",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )
    object_type: Mapped[ApprovalObjectType] = mapped_column(
        Enum(ApprovalObjectType, name="approval_object_type"), nullable=False
    )
    object_id: Mapped[UUID] = mapped_column(nullable=False)
    subject_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"), nullable=False
    )
    requested_by_agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT")
    )
    reviewed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    comment: Mapped[str | None] = mapped_column(Text)
    subject_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by_agent: Mapped[Agent | None] = relationship(foreign_keys=[requested_by_agent_id])
    reviewed_by_user: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_user_id])
