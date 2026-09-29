import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import create_worker_session_factory
from app.core.error_monitoring import report_exception
from app.services.publication_service import PublicationService
from app.workers.celery_app import celery_app

SessionFactory = async_sessionmaker[AsyncSession]


@celery_app.task(name="publish_telegram_publication")  # type: ignore[misc]
def publish_telegram_publication(publication_id: str, execution_token: str | None = None) -> None:
    asyncio.run(_run_in_worker_loop(UUID(publication_id), execution_token))


async def _run_in_worker_loop(publication_id: UUID, execution_token: str | None = None) -> None:
    engine, factory = create_worker_session_factory()
    try:
        async with factory() as session:
            await PublicationService(session).execute_telegram(
                publication_id, execution_token=execution_token
            )
    except Exception as error:
        report_exception(
            error, publication_id=str(publication_id), event="telegram_publication_failed"
        )
        raise
    finally:
        await engine.dispose()
