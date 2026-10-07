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
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, UUIDTimestampMixin


class MarketingMessageRole(StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM_EVENT = "SYSTEM_EVENT"


class MarketingMessageStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MarketingConversation(UUIDTimestampMixin, Base):
    __tablename__ = "marketing_conversations"
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), index=True
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))
    context_digest: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_marketing_conversations_updated_at", "updated_at"),)


class MarketingContextSnapshot(UUIDTimestampMixin, Base):
    __tablename__ = "marketing_context_snapshots"
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketing_conversations.id", ondelete="RESTRICT")
    )
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="RESTRICT"))
    strategy_version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    snapshot_hash: Mapped[str] = mapped_column(String(64))


class MarketingMessage(UUIDTimestampMixin, Base):
    __tablename__ = "marketing_messages"
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("marketing_conversations.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[MarketingMessageRole] = mapped_column(
        Enum(MarketingMessageRole, name="marketing_message_role")
    )
    status: Mapped[MarketingMessageStatus] = mapped_column(
        Enum(MarketingMessageStatus, name="marketing_message_status")
    )
    content: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    reply_to_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("marketing_messages.id", ondelete="RESTRICT")
    )
    context_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("marketing_context_snapshots.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    agent_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT")
    )
    references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    limitations: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    client_message_id: Mapped[UUID | None] = mapped_column()
    __table_args__ = (
        CheckConstraint(
            "role != 'USER' OR length(btrim(content)) > 0", name="marketing_user_content"
        ),
        CheckConstraint(
            "role != 'ASSISTANT' OR status != 'COMPLETED' OR length(btrim(content)) > 0",
            name="marketing_assistant_content",
        ),
        Index(
            "uq_marketing_message_client",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        Index(
            "uq_marketing_pending_turn",
            "conversation_id",
            unique=True,
            postgresql_where=text("role = 'ASSISTANT' AND status = 'PENDING'"),
        ),
    )
