import pytest

from app.core.test_database import (
    ensure_safe_test_database_url,
    resolve_test_database_url,
)


def test_runtime_url_is_derived_to_dedicated_test_database() -> None:
    resolved = resolve_test_database_url(
        "postgresql+asyncpg://iteam:iteam@postgres:5432/iteam",
        None,
    )
    assert resolved.endswith("/iteam_test")
    assert ensure_safe_test_database_url(resolved) == resolved


def test_explicit_test_database_is_allowed() -> None:
    url = "postgresql+asyncpg://iteam:iteam@postgres:5432/iteam_test"
    assert resolve_test_database_url(url, url) == url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://iteam:iteam@postgres:5432/iteam",
        "postgresql+asyncpg://iteam:iteam@postgres:5432/postgres",
    ],
)
def test_runtime_and_reserved_databases_are_rejected(url: str) -> None:
    with pytest.raises(RuntimeError, match="dedicated PostgreSQL|ending in"):
        ensure_safe_test_database_url(url)
