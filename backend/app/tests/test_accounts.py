import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import encode_access_token, verify_password
from app.models.activity import ActivityLog
from app.models.user import User
from app.tests.test_auth import create_user


@pytest.fixture(autouse=True)
def enable_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_registration_enabled", True)


def registration(**overrides: object) -> dict[str, object]:
    return {
        "email": " New@Example.com ",
        "password": "secure-password-24",
        "full_name": " New User ",
        **overrides,
    }


async def test_registration(client: AsyncClient, db_session: AsyncSession) -> None:
    response = await client.post("/api/v1/auth/register", json=registration())
    assert response.status_code == 201
    assert response.json()["role"] == "MANAGER"
    assert response.json()["email"] == "new@example.com"
    assert response.json()["full_name"] == "New User"
    assert settings.auth_cookie_name not in response.cookies
    user = await db_session.scalar(select(User).where(User.email == "new@example.com"))
    assert user is not None and user.auth_version == 1
    assert user.password_hash != "secure-password-24"
    assert verify_password("secure-password-24", user.password_hash)
    event = await db_session.scalar(select(ActivityLog).where(ActivityLog.user_id == user.id))
    assert event is not None and event.event_type == "USER_REGISTERED" and event.metadata_ == {}
    duplicate = await client.post(
        "/api/v1/auth/register", json=registration(email="NEW@EXAMPLE.COM")
    )
    assert duplicate.status_code == 409


async def test_cannot_register_admin(client: AsyncClient) -> None:
    assert (
        await client.post("/api/v1/auth/register", json=registration(role="ADMIN"))
    ).status_code == 422


async def test_disabled(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "self_registration_enabled", False)
    assert (await client.get("/api/v1/auth/capabilities")).json() == {
        "self_registration_enabled": False
    }
    assert (await client.post("/api/v1/auth/register", json=registration())).status_code == 403


@pytest.mark.parametrize("password", ["", "short", " " * 12, "x" * 129])
async def test_password_policy(client: AsyncClient, password: str) -> None:
    assert (
        await client.post("/api/v1/auth/register", json=registration(password=password))
    ).status_code == 422


async def test_inactive_duplicate(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, is_active=False)
    email = user.email
    assert (
        await client.post("/api/v1/auth/register", json=registration(email=email.upper()))
    ).status_code == 409
    assert (
        await client.post("/api/v1/auth/login", json={"email": email, "password": "valid-password"})
    ).status_code == 401


async def test_change_requires_auth(client: AsyncClient) -> None:
    assert (
        await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "valid-password", "new_password": "secure-password-24"},
        )
    ).status_code == 401


@pytest.mark.parametrize(
    ("current", "new", "expected"),
    [
        ("wrong", "secure-password-24", 400),
        ("valid-password", "valid-password", 400),
        ("valid-password", "short", 422),
    ],
)
async def test_change_rejections(
    client: AsyncClient, db_session: AsyncSession, current: str, new: str, expected: int
) -> None:
    user = await create_user(db_session)
    client.cookies.set(settings.auth_cookie_name, encode_access_token(user.id))
    response = await client.post(
        "/api/v1/auth/change-password", json={"current_password": current, "new_password": new}
    )
    assert response.status_code == expected
    await db_session.refresh(user)
    assert user.auth_version == 1
    assert verify_password("valid-password", user.password_hash)


async def test_invalidation(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    old = encode_access_token(user.id)
    client.cookies.set(settings.auth_cookie_name, old, domain="test.local", path="/")
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "valid-password", "new_password": "secure-password-24"},
    )
    assert response.status_code == 200
    replacement = response.cookies[settings.auth_cookie_name]
    assert replacement != old
    await db_session.refresh(user)
    assert user.auth_version == 2 and verify_password("secure-password-24", user.password_hash)
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    client.cookies.clear()
    client.cookies.set(settings.auth_cookie_name, old)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    client.cookies.set(settings.auth_cookie_name, replacement)
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    event = await db_session.scalar(select(ActivityLog).where(ActivityLog.user_id == user.id))
    assert (
        event is not None and event.event_type == "USER_PASSWORD_CHANGED" and event.metadata_ == {}
    )


async def test_legacy_token_requires_login(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session)
    token = jwt.encode({"sub": str(user.id)}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    client.cookies.set(settings.auth_cookie_name, token)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_inactive_password_change(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, is_active=False)
    client.cookies.set(settings.auth_cookie_name, encode_access_token(user.id))
    assert (
        await client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "valid-password", "new_password": "secure-password-24"},
        )
    ).status_code == 403
