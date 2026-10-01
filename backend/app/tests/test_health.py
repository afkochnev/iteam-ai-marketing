import pytest
from fastapi.testclient import TestClient

from alembic.config import Config
from alembic.script import ScriptDirectory
from app.api import system
from app.api.system import EXPECTED_MIGRATION_HEAD
from app.core.errors import AppError
from app.core.redaction import redact
from app.main import app


def test_readiness_expected_head_matches_alembic_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert EXPECTED_MIGRATION_HEAD == script.get_current_head()


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}
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


def test_celery_redelivery_settings_match_claimed_work_safety() -> None:
    from app.workers.celery_app import celery_app

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_serializer == "json"


def test_smm_repair_budget_is_exactly_one() -> None:
    from app.core.config import Settings

    settings = Settings(smm_agent_output_repair_attempts=1)
    settings.validate_production()
    invalid = Settings(smm_agent_output_repair_attempts=2)
    with pytest.raises(RuntimeError, match="SMM_AGENT_OUTPUT_REPAIR_ATTEMPTS"):
        invalid.validate_production()


@pytest.mark.asyncio
async def test_readiness_dependency_failures_are_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(system, "_check_database", unavailable)
    monkeypatch.setattr(system, "_check_redis", unavailable)
    with pytest.raises(AppError) as error:
        await system.readiness()
    assert error.value.code == "READINESS_CHECK_FAILED"
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_readiness_rejects_schema_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def available() -> bool:
        return True

    monkeypatch.setattr(system, "_check_database", available)
    monkeypatch.setattr(system, "_check_redis", available)
    monkeypatch.setattr(system, "_check_schema", lambda: _false_async())
    with pytest.raises(AppError) as error:
        await system.readiness()
    assert error.value.code == "READINESS_CHECK_FAILED"
    assert error.value.details["schema"] == "incompatible"


@pytest.mark.asyncio
async def test_readiness_rejects_invalid_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(system.settings, "app_env", "production")
    monkeypatch.setattr(system.settings, "app_secret", "short")
    with pytest.raises(AppError) as error:
        await system.readiness()
    assert error.value.code == "READINESS_CONFIG_INVALID"
    assert error.value.status_code == 503


async def _false_async() -> bool:
    return False
