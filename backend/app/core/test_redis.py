"""Fail-closed Redis isolation for test processes.

Tests may share a Docker network with the local runtime, but they must never
share its Redis logical database or publish Celery messages to its broker.
"""

from urllib.parse import SplitResult, urlsplit, urlunsplit

from sqlalchemy.engine import make_url

_TEST_ENVIRONMENTS = {"test", "testing"}
_DEFAULT_TEST_REDIS_DATABASE = 15


def redis_database_index(redis_url: str) -> int:
    """Return the explicit logical Redis database from a Redis URL."""

    try:
        parsed = urlsplit(redis_url)
    except ValueError as exc:
        raise RuntimeError("REDIS_URL is invalid") from exc
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("REDIS_URL is invalid") from exc
    if parsed.scheme not in {"redis", "rediss"} or not hostname or (port is not None and port == 0):
        raise RuntimeError("REDIS_URL must be a redis:// or rediss:// URL")
    path = parsed.path.removeprefix("/")
    if not path or "/" in path:
        raise RuntimeError("REDIS_URL must name an explicit logical database")
    try:
        database = int(path)
    except ValueError as exc:
        raise RuntimeError("REDIS_URL must name a numeric logical database") from exc
    if database < 0:
        raise RuntimeError("REDIS_URL logical database must be non-negative")
    return database


def is_test_context(app_env: str, database_url: str) -> bool:
    """Detect test execution from APP_ENV or the effective PostgreSQL database."""

    if app_env.lower() in _TEST_ENVIRONMENTS:
        return True
    try:
        database_name = (make_url(database_url).database or "").lower()
    except Exception:
        return False
    return database_name.endswith("_test")


def ensure_safe_test_redis_url(redis_url: str) -> str:
    """Reject DB0 and malformed URLs before a test process can use Redis."""

    if redis_database_index(redis_url) == 0:
        raise RuntimeError("Refusing test startup: REDIS_URL must use a non-zero logical database")
    return redis_url


def resolve_test_redis_url(runtime_redis_url: str, explicit_test_url: str | None) -> str:
    """Resolve a non-runtime Redis URL for readiness checks in test processes."""

    redis_database_index(runtime_redis_url)
    if explicit_test_url:
        return ensure_safe_test_redis_url(explicit_test_url)

    parsed = urlsplit(runtime_redis_url)
    isolated = SplitResult(
        parsed.scheme,
        parsed.netloc,
        f"/{_DEFAULT_TEST_REDIS_DATABASE}",
        parsed.query,
        parsed.fragment,
    )
    return ensure_safe_test_redis_url(urlunsplit(isolated))
