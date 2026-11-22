from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine as _create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.runtime_admission import is_passive_runtime, require_active_runtime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def create_async_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """The shared boundary for business engines, including task-local engines."""
    require_active_runtime()
    return _create_async_engine(url, **kwargs)


def create_readonly_engine() -> AsyncEngine:
    """Bounded health probe; the server starts each transaction readonly."""
    return _create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={
            "timeout": 5,
            "server_settings": {
                "default_transaction_read_only": "on",
                "statement_timeout": "5000",
            },
        },
    )


class _BusinessSession(AsyncSession):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        require_active_runtime()
        super().__init__(*args, **kwargs)


# Keep the module importable for models and migration health bootstraps, but
# never hand business callers a session in a passive process. Even direct
# connection access to the passive engine defaults to readonly transactions.
engine = (
    create_readonly_engine()
    if is_passive_runtime()
    else create_async_engine(settings.database_url, pool_pre_ping=True)
)
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, class_=_BusinessSession, expire_on_commit=False
)


def create_worker_session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create DB resources owned by one Celery event-loop invocation.

    Celery tasks are synchronous entrypoints and historically called
    ``asyncio.run`` for every delivery.  Reusing the application's pooled
    AsyncEngine across those loops lets asyncpg futures escape their loop.
    A short-lived, NullPool engine is deliberately used for task-local work;
    it is disposed by the caller before the loop exits.
    """

    worker_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    return worker_engine, async_sessionmaker(worker_engine, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDTimestampMixin:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


async def get_db_session() -> AsyncIterator[AsyncSession]:
    require_active_runtime()
    async with async_session_factory() as session:
        yield session
