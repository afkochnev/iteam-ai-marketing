import pytest
from fastapi.testclient import TestClient

from app.api import system
from app.core.errors import AppError
from app.core.redaction import redact
from app.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers.get("x-request-id")


def test_request_id_is_preserved_when_safe() -> None:
    response = TestClient(app).get("/health", headers={"X-Request-ID": "diagnostic-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "diagnostic-123"


def test_request_id_rejects_unsafe_value() -> None:
    response = TestClient(app).get("/health", headers={"X-Request-ID": "x" * 101})

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "x" * 101


def test_redaction_removes_secrets_from_nested_context() -> None:
    value = redact(
        {
            "Authorization": "Bearer secret-token",
            "password": "pw",
            "OPENAI_API_KEY": "sk-secret",
            "nested": {"JWT_SECRET": "jwt", "DATABASE_URL": "postgresql://u:pw@db/app"},
            "REDIS_URL": "redis://:pw@redis:6379/0",
        }
    )

    rendered = str(value)
    assert "secret-token" not in rendered
    assert "sk-secret" not in rendered
    assert "postgresql://u:pw" not in rendered
    assert "redis://:pw" not in rendered


@pytest.mark.asyncio
async def test_readiness_dependency_failures_are_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(system, "_check_database", unavailable)
    monkeypatch.setattr(system, "_check_redis", unavailable)
    with pytest.raises(AppError) as error:
        await system.readiness()
    assert error.value.code == "READINESS_CHECK_FAILED"
    assert error.value.status_code == 503
