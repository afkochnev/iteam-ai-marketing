import asyncio
from uuid import UUID

from app.core.database import create_worker_session_factory
from app.core.error_monitoring import report_exception
from app.services.publication_service import PublicationService
from app.workers.celery_app import celery_app


@celery_app.task(name="publish_vk_publication")  # type: ignore[misc]
def publish_vk_publication(publication_id: str) -> None:
    asyncio.run(_run(UUID(publication_id)))


async def _run(publication_id: UUID) -> None:
    engine, factory = create_worker_session_factory()
    try:
        async with factory() as session:
            await PublicationService(session).execute_vk(publication_id)
    except Exception as error:
        report_exception(error, publication_id=str(publication_id), event="vk_publication_failed")
        raise
    finally:
        await engine.dispose()
