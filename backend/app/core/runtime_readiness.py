"""Readonly readiness for the health-only passive bootstrap.

No business routers, models, workers, recovery jobs or provider clients are
imported here. PostgreSQL checks run in a transaction that is verified readonly;
Redis is contacted only for PING. Neither successful probe admits business work.
"""

from app.core.config import settings
from app.core.runtime_admission import admission_status

EXPECTED_MIGRATION_HEAD = "20261123_0031"


async def _check_readonly_database() -> tuple[bool, bool]:
    from sqlalchemy import text

    from app.core.database import create_readonly_engine

    engine = create_readonly_engine()
    try:
        async with engine.connect() as connection, connection.begin():
            readonly = await connection.scalar(text("SHOW transaction_read_only"))
            if readonly != "on":
                return False, False
            await connection.execute(text("SELECT 1"))
            try:
                rows = (
                    (await connection.execute(text("SELECT version_num FROM alembic_version")))
                    .scalars()
                    .all()
                )
            except Exception:
                return True, False
        return True, len(rows) == 1 and rows[0] == EXPECTED_MIGRATION_HEAD
    except Exception:
        return False, False
    finally:
        await engine.dispose()


async def _check_redis_ping() -> bool:
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5)
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()
    except Exception:
        return False


async def passive_readiness() -> dict[str, object]:
    try:
        database_ok, schema_ok = await _check_readonly_database()
    except Exception:
        database_ok, schema_ok = False, False
    redis_ok = await _check_redis_ping()
    return {
        "status": "ok" if database_ok and redis_ok and schema_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        "schema": "ok" if schema_ok else "incompatible",
        "database_read_only": database_ok,
        **admission_status(),
    }
