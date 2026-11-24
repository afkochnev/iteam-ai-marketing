from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UUIDTimestampMixin
from app.models.user import User

DEFAULT_WORKSPACE_ID = UUID("4183ac32-0f61-54da-ad9e-ebb31696101b")


class WorkspaceRole(StrEnum):
    WORKSPACE_ADMIN = "WORKSPACE_ADMIN"
    MARKETING_MANAGER = "MARKETING_MANAGER"


class Workspace(UUIDTimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    memberships: Mapped[list[WorkspaceMembership]] = relationship(
        back_populates="workspace", lazy="raise"
    )

    __table_args__ = (
        Index("ix_workspaces_slug", func.lower(slug), unique=True),
        Index(
            "uq_workspaces_one_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )


class WorkspaceMembership(UUIDTimestampMixin, Base):
    __tablename__ = "workspace_memberships"

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, name="workspace_role"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    workspace: Mapped[Workspace] = relationship(back_populates="memberships", lazy="raise")
    user: Mapped[User] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_memberships_workspace_user"),
        Index("ix_workspace_memberships_workspace_id", "workspace_id"),
        Index("ix_workspace_memberships_user_id", "user_id"),
        Index("ix_workspace_memberships_role", "role"),
    )
