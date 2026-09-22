import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services.task_dispatcher_service import TaskDispatcherService
from app.workers.celery_app import celery_app


@celery_app.task(name="dispatch_ready_tasks")  # type: ignore[misc]
def dispatch_ready_tasks() -> None:
    asyncio.run(_dispatch())


async def _dispatch() -> None:
    # Embedded Celery beat executes each invocation on a fresh asyncio loop.
    # A loop-local NullPool prevents asyncpg connections from crossing loops.
    loop_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(loop_engine, expire_on_commit=False)
        async with session_factory() as session:
            await TaskDispatcherService(session).dispatch_ready_tasks()
    finally:
        await loop_engine.dispose()
