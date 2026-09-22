from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.rate_limit import SimpleRateLimitMiddleware


@pytest.fixture(autouse=True)
def reset_rate_limit_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_ai_actions_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_uploads_per_minute", 1)
    SimpleRateLimitMiddleware.reset_for_tests()
    yield
    SimpleRateLimitMiddleware.reset_for_tests()


async def _assert_429(client: AsyncClient, path: str, **kwargs: object) -> None:
    first = await client.post(path, **kwargs)
    second = await client.post(path, **kwargs)
    assert first.status_code != 429
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"
    assert "message" in second.json()["error"]


async def test_login_rate_limit_is_api_level(client: AsyncClient) -> None:
    await _assert_429(
        client,
        "/api/v1/auth/login",
        json={"email": f"{uuid4()}@example.com", "password": "invalid"},
    )


async def test_ai_run_rate_limit_blocks_application_path(client: AsyncClient) -> None:
    await _assert_429(client, f"/api/v1/tasks/{uuid4()}/run")


async def test_retry_rate_limit_blocks_application_path(client: AsyncClient) -> None:
    await _assert_429(client, f"/api/v1/tasks/{uuid4()}/retry")


async def test_upload_rate_limit_blocks_upload_application_path(client: AsyncClient) -> None:
    await _assert_429(
        client,
        "/api/v1/knowledge/upload",
        files={"file": ("note.txt", b"small", "text/plain")},
    )
