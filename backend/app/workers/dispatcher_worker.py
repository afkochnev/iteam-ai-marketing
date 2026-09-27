import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.error_monitoring import report_exception
from app.models.publication import Publication, PublicationStatus
from app.services.publication_service import PublicationService
from app.services.task_dispatcher_service import TaskDispatcherService
from app.workers.celery_app import celery_app
from app.workers.telegram_worker import publish_telegram_publication
from app.workers.vk_worker import publish_vk_publication


@celery_app.task(name="dispatch_ready_tasks")  # type: ignore[misc]
def dispatch_ready_tasks() -> None:
    asyncio.run(_dispatch())


@celery_app.task(name="dispatch_due_publications")  # type: ignore[misc]
def dispatch_due_publications() -> None:
    asyncio.run(_dispatch_publications())


async def _dispatch() -> None:
    # Embedded Celery beat executes each invocation on a fresh asyncio loop.
    # A loop-local NullPool prevents asyncpg connections from crossing loops.
    loop_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(loop_engine, expire_on_commit=False)
        async with session_factory() as session:
            await TaskDispatcherService(session).dispatch_ready_tasks()
    except Exception as error:
        report_exception(error, event="dispatcher_failed")
        raise
    finally:
        await loop_engine.dispose()


async def _dispatch_publications() -> None:
    loop_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(loop_engine, expire_on_commit=False)
        async with factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Publication.id)
                        .where(
                            Publication.status == PublicationStatus.SCHEDULED,
                            Publication.scheduled_at <= datetime.now(UTC),
                        )
                        .with_for_update(skip_locked=True)
                        .limit(20)
                    )
                ).all()
            )
            for publication_id in rows:
                try:
                    await PublicationService(session).claim_for_publish(publication_id)
                    row = await session.get(Publication, publication_id)
                    if row is not None and row.channel.value == "VK":
                        publish_vk_publication.delay(str(publication_id))
                    else:
                        publish_telegram_publication.delay(str(publication_id))
                except Exception:
                    await session.rollback()
    except Exception as error:
        if isinstance(error, ProgrammingError) and "publications" in str(error).lower():
            return
        report_exception(error, event="publication_dispatch_failed")
        raise
    finally:
        await loop_engine.dispose()
