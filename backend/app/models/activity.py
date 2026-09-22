from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin


class ActivityLog(UUIDTimestampMixin, Base):
    __tablename__ = "activity_logs"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_activity_logs_operation_key"),
        Index("ix_activity_logs_campaign_id", "campaign_id"),
        Index("ix_activity_logs_created_at", "created_at"),
        Index("ix_activity_logs_event_type", "event_type"),
    )

    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    campaign_id: Mapped[UUID | None] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    content_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL")
    )
    approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL")
    )
    operation_key: Mapped[str | None] = mapped_column(String(180))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
