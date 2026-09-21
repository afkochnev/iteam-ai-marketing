from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin
from app.models.agent import Agent
from app.models.task import Task


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ToolCallStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AgentRun(UUIDTimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_task_id", "task_id"),
        Index("ix_agent_runs_agent_id", "agent_id"),
        Index("ix_agent_runs_campaign_id", "campaign_id"),
        Index("ix_agent_runs_status", "status"),
        Index("ix_agent_runs_created_at", "created_at"),
        Index(
            "uq_agent_runs_active_task",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL')"),
        ),
    )
    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, name="agent_run_status"), nullable=False
    )
    input_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    openai_response_id: Mapped[str | None] = mapped_column(String(255))
    trace_id: Mapped[str | None] = mapped_column(String(255))
    queue_job_id: Mapped[str | None] = mapped_column(String(255))
    request_count: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent: Mapped[Agent] = relationship(lazy="raise")
    task: Mapped[Task] = relationship(lazy="raise")
    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan", lazy="raise"
    )


class ToolCall(UUIDTimestampMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_agent_run_id", "agent_run_id"),
        Index("ix_tool_calls_status", "status"),
    )
    agent_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[ToolCallStatus] = mapped_column(
        Enum(ToolCallStatus, name="tool_call_status"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_run: Mapped[AgentRun] = relationship(back_populates="tool_calls")
