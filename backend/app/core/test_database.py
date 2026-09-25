"""Safety helpers for PostgreSQL-backed test isolation.

The application database is intentionally never used by destructive pytest
fixtures.  These helpers validate the effective URL before the test engine is
constructed, so a misconfigured test run fails before any cleanup SQL runs.
"""

from sqlalchemy.engine import URL, make_url

_RESERVED_DATABASE_NAMES = {"iteam", "postgres", "production", "prod"}


def ensure_safe_test_database_url(database_url: str) -> str:
    """Return *database_url* only when it names an explicit PostgreSQL test DB."""

    try:
        parsed = make_url(database_url)
    except Exception as exc:  # pragma: no cover - parser details are implementation-specific
        raise RuntimeError("TEST_DATABASE_URL is invalid") from exc

    database_name = (parsed.database or "").lower()
    if parsed.drivername.split("+")[0] != "postgresql":
        raise RuntimeError("Backend tests require a dedicated PostgreSQL database")
    if database_name in _RESERVED_DATABASE_NAMES or not database_name.endswith("_test"):
        raise RuntimeError(
            "Refusing destructive test setup: TEST_DATABASE_URL must target "
            "a database ending in '_test'"
        )
    return database_url


def resolve_test_database_url(runtime_database_url: str, explicit_test_url: str | None) -> str:
    """Resolve the isolated test URL, deriving ``*_test`` from runtime config when needed."""

    if explicit_test_url:
        return ensure_safe_test_database_url(explicit_test_url)

    try:
        runtime_url = make_url(runtime_database_url)
    except Exception as exc:  # pragma: no cover - parser details are implementation-specific
        raise RuntimeError("DATABASE_URL is invalid") from exc
    test_url: URL = runtime_url.set(database="iteam_test")
    return ensure_safe_test_database_url(str(test_url))
