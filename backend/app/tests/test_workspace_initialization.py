from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.models.workspace import Workspace, WorkspaceMembership, WorkspaceRole


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "20261121_0030_initialize_default_workspace.py"
    )
    spec = spec_from_file_location("workspace_initialization_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _load_migration()


async def _create_user(
    session: AsyncSession,
    *,
    role: UserRole,
    is_active: bool = True,
) -> User:
    user = User(
        email=f"workspace-bootstrap-{uuid4()}@example.com",
        password_hash="test-only",
        full_name="Workspace Bootstrap User",
        role=role,
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user


async def _run(session: AsyncSession, function) -> None:
    connection = await session.connection()
    await connection.run_sync(function)


async def test_initialization_is_deterministic_idempotent_and_reversible(
    db_session: AsyncSession,
) -> None:
    admin = await _create_user(db_session, role=UserRole.ADMIN)
    manager = await _create_user(db_session, role=UserRole.MANAGER)
    inactive = await _create_user(
        db_session,
        role=UserRole.MANAGER,
        is_active=False,
    )

    await _run(db_session, MIGRATION._initialize_default_workspace)
    await _run(db_session, MIGRATION._initialize_default_workspace)

    workspace = await db_session.scalar(
        select(Workspace).where(Workspace.id == MIGRATION.DEFAULT_WORKSPACE_ID)
    )
    assert workspace is not None
    assert workspace.name == MIGRATION.DEFAULT_WORKSPACE_NAME
    assert workspace.slug == MIGRATION.DEFAULT_WORKSPACE_SLUG
    assert workspace.is_default is True
    assert workspace.is_active is True

    memberships = list(
        await db_session.scalars(
            select(WorkspaceMembership)
            .where(WorkspaceMembership.workspace_id == workspace.id)
            .order_by(WorkspaceMembership.user_id)
        )
    )
    assert len(memberships) == 2
    by_user = {membership.user_id: membership for membership in memberships}
    assert set(by_user) == {admin.id, manager.id}
    assert inactive.id not in by_user
    assert by_user[admin.id].id == MIGRATION._membership_id(admin.id)
    assert by_user[admin.id].role is WorkspaceRole.WORKSPACE_ADMIN
    assert by_user[manager.id].id == MIGRATION._membership_id(manager.id)
    assert by_user[manager.id].role is WorkspaceRole.MARKETING_MANAGER
    assert all(membership.is_active for membership in memberships)

    await _run(db_session, MIGRATION._remove_default_workspace)

    assert await db_session.scalar(select(func.count()).select_from(Workspace)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceMembership)) == 0
    assert await db_session.scalar(select(func.count()).select_from(User)) == 3


async def test_initialization_fails_closed_on_conflicting_workspace(
    db_session: AsyncSession,
) -> None:
    db_session.add(Workspace(name="Conflicting", slug="iteam", is_default=False))
    await db_session.flush()

    with pytest.raises(RuntimeError, match="Conflicting workspace state"):
        await _run(db_session, MIGRATION._initialize_default_workspace)

    assert await db_session.scalar(select(func.count()).select_from(Workspace)) == 1
    assert await db_session.scalar(select(func.count()).select_from(WorkspaceMembership)) == 0


async def test_initialization_fails_closed_on_conflicting_membership(
    db_session: AsyncSession,
) -> None:
    user = await _create_user(db_session, role=UserRole.ADMIN)
    workspace = Workspace(
        id=MIGRATION.DEFAULT_WORKSPACE_ID,
        name=MIGRATION.DEFAULT_WORKSPACE_NAME,
        slug=MIGRATION.DEFAULT_WORKSPACE_SLUG,
        is_default=True,
        is_active=True,
    )
    db_session.add(workspace)
    await db_session.flush()
    conflicting = WorkspaceMembership(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.MARKETING_MANAGER,
        is_active=True,
    )
    db_session.add(conflicting)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="Conflicting workspace membership state"):
        await _run(db_session, MIGRATION._initialize_default_workspace)

    memberships = list(await db_session.scalars(select(WorkspaceMembership)))
    assert len(memberships) == 1
    assert memberships[0].id == conflicting.id
    assert memberships[0].role is WorkspaceRole.MARKETING_MANAGER
