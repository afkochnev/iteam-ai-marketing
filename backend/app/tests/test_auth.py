from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import encode_access_token, hash_password
from app.models.user import User, UserRole
from app.repositories.users import UserRepository


async def create_user(
    session: AsyncSession, *, role: UserRole = UserRole.ADMIN, is_active: bool = True
) -> User:
    user = await UserRepository(session).create(
        email=f"{role.value.lower()}-{uuid4()}@example.com",
        password_hash=hash_password("valid-password"),
        full_name="Test User",
        role=role,
        is_active=is_active,
    )
    await session.commit()
    return user


async def test_login_me_and_logout(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email.upper(), "password": "valid-password"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["user"]["email"] == user.email
    assert settings.auth_cookie_name in response.cookies
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    me_response = await client.get("/api/v1/auth/me")
    assert me_response.status_code == status.HTTP_200_OK
    assert me_response.json()["id"] == str(user.id)
    assert (await client.post("/api/v1/auth/logout")).status_code == status.HTTP_200_OK
    assert (await client.get("/api/v1/auth/me")).status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.parametrize(
    ("email", "password"),
    [("missing@example.com", "valid-password"), ("USER_EMAIL", "wrong-password")],
)
async def test_login_rejects_invalid_credentials(
    client: AsyncClient, db_session: AsyncSession, email: str, password: str
) -> None:
    user = await create_user(db_session)
    actual_email = user.email if email == "USER_EMAIL" else email
    response = await client.post(
        "/api/v1/auth/login", json={"email": actual_email, "password": password}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_inactive_user_cannot_login(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, is_active=False)
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "valid-password"}
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_me_requires_valid_token(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/me")).status_code == status.HTTP_401_UNAUTHORIZED
    client.cookies.set(settings.auth_cookie_name, "invalid-token")
    assert (await client.get("/api/v1/auth/me")).status_code == status.HTTP_401_UNAUTHORIZED


async def test_me_rejects_inactive_user(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, is_active=False)
    client.cookies.set(settings.auth_cookie_name, encode_access_token(user.id))
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["error"]["code"] == "USER_INACTIVE"


async def test_require_admin_allows_admin_and_rejects_manager() -> None:
    admin = User(email="admin@example.com", password_hash="x", role=UserRole.ADMIN)
    manager = User(email="manager@example.com", password_hash="x", role=UserRole.MANAGER)
    assert await require_admin(admin) is admin
    with pytest.raises(AppError) as error:
        await require_admin(manager)
    assert error.value.status_code == status.HTTP_403_FORBIDDEN


async def test_admin_changes_email_with_same_password_and_role(client, db_session):
    user = await create_user(db_session)
    previous = user.email
    password_hash = user.password_hash
    await client.post("/api/v1/auth/login", json={"email": previous, "password": "valid-password"})
    old_cookie = client.cookies.get(settings.auth_cookie_name)
    denied = await client.post(
        "/api/v1/auth/change-email",
        json={"email": "afkochnev@gmail.com", "current_password": "wrong"},
    )
    assert denied.status_code == 400
    result = await client.post(
        "/api/v1/auth/change-email",
        json={"email": " AFKOCHNEV@gmail.com ", "current_password": "valid-password"},
    )
    assert result.status_code == 200
    revoked = await client.get(
        "/api/v1/auth/me", headers={"Cookie": f"{settings.auth_cookie_name}={old_cookie}"}
    )
    assert revoked.status_code == 401
    await db_session.refresh(user)
    assert user.email == "afkochnev@gmail.com"
    assert user.role is UserRole.ADMIN and user.password_hash == password_hash
    assert (await client.get("/api/v1/auth/me")).json()["email"] == user.email
    assert (
        await client.post(
            "/api/v1/auth/login", json={"email": previous, "password": "valid-password"}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "valid-password"}
        )
    ).status_code == 200


async def test_manager_cannot_change_admin_identity(client, db_session):
    user = await create_user(db_session, role=UserRole.MANAGER)
    await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "valid-password"}
    )
    result = await client.post(
        "/api/v1/auth/change-email",
        json={"email": "afkochnev@gmail.com", "current_password": "valid-password"},
    )
    assert result.status_code == 403


async def test_admin_email_collision_preserves_original_identity(client, db_session):
    user = await create_user(db_session)
    other = await create_user(db_session, role=UserRole.MANAGER)
    original = user.email
    await client.post("/api/v1/auth/login", json={"email": original, "password": "valid-password"})
    result = await client.post(
        "/api/v1/auth/change-email",
        json={"email": other.email, "current_password": "valid-password"},
    )
    assert result.status_code == 409
    await db_session.refresh(user)
    assert user.email == original and user.role is UserRole.ADMIN
    assert (await client.get("/api/v1/auth/me")).status_code == 200
