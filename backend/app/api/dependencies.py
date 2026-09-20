from typing import Annotated

import jwt
from fastapi import Cookie, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db_session
from app.core.errors import AppError
from app.core.security import decode_access_token
from app.models.user import User, UserRole
from app.repositories.users import UserRepository

SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user(
    session: SessionDependency,
    access_token: Annotated[str | None, Cookie(alias=settings.auth_cookie_name)] = None,
) -> User:
    if access_token is None:
        raise AppError(
            "AUTHENTICATION_REQUIRED", "Требуется авторизация.", status.HTTP_401_UNAUTHORIZED
        )
    try:
        user_id = decode_access_token(access_token)
    except (jwt.InvalidTokenError, ValueError):
        raise AppError(
            "AUTHENTICATION_REQUIRED", "Требуется авторизация.", status.HTTP_401_UNAUTHORIZED
        ) from None
    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        raise AppError(
            "AUTHENTICATION_REQUIRED", "Требуется авторизация.", status.HTTP_401_UNAUTHORIZED
        )
    if not user.is_active:
        raise AppError("USER_INACTIVE", "Пользователь деактивирован.", status.HTTP_403_FORBIDDEN)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(current_user: CurrentUser) -> User:
    if current_user.role is not UserRole.ADMIN:
        raise AppError("FORBIDDEN", "Недостаточно прав.", status.HTTP_403_FORBIDDEN)
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]
