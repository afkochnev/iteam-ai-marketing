import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services.task_recovery_service import TaskRecoveryService
from app.workers.celery_app import celery_app


@celery_app.task(name="recover_stuck_tasks")  # type: ignore[misc]
def recover_stuck_tasks() -> None:
    asyncio.run(_recover())


async def _recover() -> None:
    loop_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(loop_engine, expire_on_commit=False)
        async with factory() as session:
            await TaskRecoveryService(session).recover_stuck()
    finally:
        await loop_engine.dispose()
