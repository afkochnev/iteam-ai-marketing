from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin


class AgentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class AgentSlug(StrEnum):
    MARKETING_DIRECTOR = "marketing_director"
    KNOWLEDGE_KEEPER = "knowledge_keeper"
    WRITER = "writer"
    SMM_MANAGER = "smm_manager"


class AgentRole(StrEnum):
    MARKETING_DIRECTOR = "marketing_director"
    KNOWLEDGE_KEEPER = "knowledge_keeper"
    CONTENT_WRITER = "content_writer"
    SMM_MANAGER = "smm_manager"


class Agent(UUIDTimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "autonomy_level >= 0 AND autonomy_level <= 5",
            name="autonomy_level_range",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status"),
        nullable=False,
        default=AgentStatus.ACTIVE,
    )
    autonomy_level: Mapped[int] = mapped_column(nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    tools: Mapped[list[AgentTool]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AgentTool.tool_name",
    )


class AgentTool(UUIDTimestampMixin, Base):
    __tablename__ = "agent_tools"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_name", name="uq_agent_tools_agent_id_tool_name"),
    )

    agent_id: Mapped[UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    agent: Mapped[Agent] = relationship(back_populates="tools")
