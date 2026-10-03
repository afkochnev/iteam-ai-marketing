import pytest

from app.core.config import Settings, settings
from app.core.test_redis import (
    ensure_safe_test_redis_url,
    redis_database_index,
    resolve_test_redis_url,
)


def test_runtime_url_is_derived_to_dedicated_test_database() -> None:
    resolved = resolve_test_redis_url("redis://redis:6379/0", None)
    assert resolved == "redis://redis:6379/15"
    assert redis_database_index(resolved) == 15


def test_explicit_test_redis_database_is_allowed() -> None:
    runtime_url = "redis://redis:6379/0"
    test_url = "redis://redis:6379/12"
    assert resolve_test_redis_url(runtime_url, test_url) == test_url


@pytest.mark.parametrize(
    "url",
    [
        "redis://redis:6379/0",
        "redis://redis:6379",
        "redis://redis:6379/not-a-number",
        "redis://redis:not-a-port/15",
        "http://redis:6379/15",
    ],
)
def test_runtime_or_malformed_redis_urls_are_rejected(url: str) -> None:
    with pytest.raises(RuntimeError, match="REDIS_URL|non-zero|numeric"):
        ensure_safe_test_redis_url(url)


@pytest.mark.parametrize(
    ("app_env", "database_url"),
    [
        ("test", "postgresql+asyncpg://iteam:iteam@postgres:5432/iteam"),
        ("development", "postgresql+asyncpg://iteam:iteam@postgres:5432/iteam_test"),
    ],
)
def test_test_context_with_runtime_redis_db_zero_fails_closed(
    app_env: str,
    database_url: str,
) -> None:
    unsafe = Settings(
        app_env=app_env,
        database_url=database_url,
        redis_url="redis://redis:6379/0",
    )
    assert unsafe.is_test_context is True
    with pytest.raises(RuntimeError, match="non-zero"):
        unsafe.validate_redis_isolation()


def test_runtime_celery_keeps_configured_redis_transport() -> None:
    runtime = Settings(
        app_env="development",
        database_url="postgresql+asyncpg://iteam:iteam@postgres:5432/iteam",
        redis_url="redis://redis:6379/0",
    )
    runtime.validate_redis_isolation()
    assert runtime.celery_broker_url == runtime.redis_url
    assert runtime.celery_result_backend_url == runtime.redis_url


def test_pytest_celery_transport_cannot_reach_redis() -> None:
    from app.workers.celery_app import celery_app

    assert settings.is_test_context is True
    assert redis_database_index(settings.redis_url) != 0
    assert celery_app.conf.broker_url == "memory://"
    assert celery_app.conf.result_backend == "cache+memory://"

    queued = celery_app.signature("test.redis_isolation_probe").apply_async()
    assert queued.id
