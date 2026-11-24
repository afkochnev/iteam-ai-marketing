from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Cookie, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory, get_db_session
from app.core.errors import AppError
from app.core.security import decode_access_token, decode_auth_version
from app.models.user import User, UserRole
from app.repositories.users import UserRepository

SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


async def get_read_snapshot_session() -> AsyncIterator[AsyncSession]:
    """Give read projections one PostgreSQL snapshot without write access."""
    async with async_session_factory(autoflush=False) as session:
        await session.connection(
            execution_options={
                "isolation_level": "REPEATABLE READ",
                "postgresql_readonly": True,
            }
        )
        yield session


ReadSnapshotSession = Annotated[AsyncSession, Depends(get_read_snapshot_session)]


async def _get_access_identity(
    access_token: Annotated[str | None, Cookie(alias=settings.auth_cookie_name)] = None,
) -> tuple[UUID, int]:
    if access_token is None:
        raise AppError(
            "AUTHENTICATION_REQUIRED", "Требуется авторизация.", status.HTTP_401_UNAUTHORIZED
        )
    try:
        return decode_access_token(access_token), decode_auth_version(access_token)
    except (jwt.InvalidTokenError, ValueError):
        raise AppError(
            "AUTHENTICATION_REQUIRED", "Требуется авторизация.", status.HTTP_401_UNAUTHORIZED
        ) from None


AccessIdentity = Annotated[tuple[UUID, int], Depends(_get_access_identity)]


async def get_current_user(identity: AccessIdentity, session: SessionDependency) -> User:
    return await _authenticate_user(session, *identity)


async def get_current_snapshot_user(
    identity: AccessIdentity,
    session: ReadSnapshotSession,
) -> User:
    return await _authenticate_user(session, *identity)


async def _authenticate_user(session: AsyncSession, user_id: UUID, auth_version: int) -> User:
    user = await UserRepository(session).get_by_id(user_id)
    if user is None or user.auth_version != auth_version:
        raise AppError(
            "AUTHENTICATION_REQUIRED", "Требуется авторизация.", status.HTTP_401_UNAUTHORIZED
        )
    if not user.is_active:
        raise AppError("USER_INACTIVE", "Пользователь деактивирован.", status.HTTP_403_FORBIDDEN)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentSnapshotUser = Annotated[User, Depends(get_current_snapshot_user)]


async def require_admin(current_user: CurrentUser) -> User:
    if current_user.role is not UserRole.ADMIN:
        raise AppError("FORBIDDEN", "Недостаточно прав.", status.HTTP_403_FORBIDDEN)
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]
