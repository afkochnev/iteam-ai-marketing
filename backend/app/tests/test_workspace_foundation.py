from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceMembership, WorkspaceRole


async def _create_user(session: AsyncSession, *, role: UserRole = UserRole.ADMIN) -> User:
    user = User(
        email=f"workspace-{uuid4()}@example.com",
        password_hash="test-only",
        full_name="Workspace Test User",
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


async def test_workspace_membership_persists_exact_role(db_session: AsyncSession) -> None:
    user = await _create_user(db_session)
    workspace = Workspace(name="iTeam", slug="iteam", is_default=True)
    db_session.add(workspace)
    await db_session.flush()

    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.WORKSPACE_ADMIN,
    )
    db_session.add(membership)
    await db_session.commit()

    stored = await db_session.scalar(
        select(WorkspaceMembership).where(WorkspaceMembership.id == membership.id)
    )
    assert stored is not None
    assert stored.workspace_id == workspace.id
    assert stored.user_id == user.id
    assert stored.role is WorkspaceRole.WORKSPACE_ADMIN
    assert stored.is_active is True


async def test_workspace_membership_is_unique_per_user_and_workspace(
    db_session: AsyncSession,
) -> None:
    user = await _create_user(db_session, role=UserRole.MANAGER)
    workspace = Workspace(name="iTeam", slug="iteam")
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.MARKETING_MANAGER,
        )
    )
    await db_session.flush()

    db_session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.WORKSPACE_ADMIN,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_workspace_slug_is_unique_case_insensitively(db_session: AsyncSession) -> None:
    db_session.add(Workspace(name="iTeam", slug="iteam"))
    await db_session.flush()
    db_session.add(Workspace(name="Duplicate", slug="ITEAM"))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_only_one_default_workspace_is_allowed(db_session: AsyncSession) -> None:
    db_session.add(Workspace(name="iTeam", slug="iteam", is_default=True))
    await db_session.flush()
    db_session.add(Workspace(name="Other", slug="other", is_default=True))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
