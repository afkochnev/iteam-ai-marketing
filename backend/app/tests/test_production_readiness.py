import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.redaction import redact
from app.main import app


def test_production_configuration_rejects_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "app_secret", "short")
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        settings.validate_production()


def test_production_configuration_accepts_secure_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "app_env": "production",
        "app_secret": "a" * 40,
        "jwt_secret": "b" * 40,
        "admin_password": "c" * 40,
        "database_url": "postgresql+asyncpg://user:password@db/app",
        "redis_url": "redis://:password@redis:6379/0",
        "openai_api_key": "sk-test",
        "openai_default_model": "gpt-test",
        "frontend_url": "https://marketing.example.com",
        "cookie_secure": True,
        "allowed_hosts": "marketing.example.com",
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    settings.validate_production()


def test_configuration_rejects_invalid_numeric_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_run_timeout_seconds", 0)
    with pytest.raises(RuntimeError, match="AGENT_RUN_TIMEOUT_SECONDS"):
        settings.validate_production()


def test_security_headers_and_production_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.json()["version"] == settings.app_version


def test_redaction_removes_supported_secret_shapes() -> None:
    value = redact(
        {
            "Authorization": "Bearer test-token",
            "password": "db-password",
            "OPENAI_API_KEY": "sk-secret",
            "JWT_SECRET": "jwt-secret",
            "DATABASE_URL": "postgresql://user:db-password@db/app",
            "REDIS_URL": "redis://:redis-password@redis:6379/0",
            "nested": {"access_token": "nested-token"},
        }
    )
    rendered = repr(value)
    for secret in (
        "test-token",
        "db-password",
        "sk-secret",
        "jwt-secret",
        "redis-password",
        "nested-token",
    ):
        assert secret not in rendered
