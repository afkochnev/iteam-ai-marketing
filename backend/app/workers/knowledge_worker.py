import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import create_worker_session_factory
from app.core.error_monitoring import report_exception
from app.models.knowledge import KnowledgeItem, KnowledgeItemStatus
from app.repositories.knowledge import KnowledgeRepository
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


class _ProviderFailure(Exception):
    """An expected provider-side failure that is persisted as item failure."""


@celery_app.task(name="index_knowledge_item")  # type: ignore[misc]
def index_knowledge_item(knowledge_item_id: str) -> None:
    asyncio.run(_run_in_worker_loop(UUID(knowledge_item_id)))


async def _run_in_worker_loop(item_id: UUID) -> None:
    """Run one delivery with DB resources owned by this asyncio loop."""

    worker_engine, factory = create_worker_session_factory()
    try:
        await _index(item_id, factory)
    finally:
        await worker_engine.dispose()


async def _index(item_id: UUID, session_factory: SessionFactory | None = None) -> None:
    factory = session_factory
    if factory is None:
        # Direct application/test calls already run inside the caller's loop.
        # Celery always uses _run_in_worker_loop above.
        from app.core.database import async_session_factory

        factory = async_session_factory
    async with factory() as session:
        repository = KnowledgeRepository(session)
        item = await repository.get_item(item_id, lock=True)
        if item is None or item.status != KnowledgeItemStatus.INDEXING:
            return
        store = await repository.active_store()
        if store is None:
            await _fail(
                item_id,
                "KNOWLEDGE_STORE_NOT_INITIALIZED",
                "База знаний не инициализирована.",
                factory,
            )
            return
        store_id = store.external_store_id
        file_id = item.openai_file_id
        source_content = item.source_content
        filename = item.original_filename or item.title
        content_type = item.content_type
        vector_file_id = item.vector_store_file_id
        source_id = item.source_id
        # Release the row lock before any external OpenAI call. Retry
        # reconciliation below uses separate short transactions.
        await session.rollback()

    try:
        from app.integrations.openai_knowledge import OpenAIKnowledgeProvider

        try:
            provider_client = OpenAIKnowledgeProvider()
        except Exception as error:
            raise _ProviderFailure from error
        async with asyncio.timeout(settings.knowledge_index_timeout_seconds):
            if file_id is None:
                if source_content is None:
                    await _fail(
                        item_id,
                        "KNOWLEDGE_FILE_ID_MISSING",
                        "OpenAI File ID отсутствует и исходный файл недоступен.",
                        factory,
                    )
                    return
                try:
                    file_id = await provider_client.upload_file(filename, source_content)
                except Exception as error:
                    raise _ProviderFailure from error
                async with factory() as session:
                    current = await KnowledgeRepository(session).get_item(item_id, lock=True)
                    if current and current.status == KnowledgeItemStatus.INDEXING:
                        current.openai_file_id = file_id
                        await session.commit()
            if vector_file_id is None:
                try:
                    vector_file_id, status = await provider_client.attach_file(
                        store_id,
                        file_id,
                        {
                            "knowledge_item_id": str(item_id),
                            "source_id": str(source_id),
                            "content_type": content_type,
                            "is_active": True,
                        },
                    )
                except Exception as error:
                    raise _ProviderFailure from error
                async with factory() as session:
                    current = await KnowledgeRepository(session).get_item(item_id, lock=True)
                    if current and current.status == KnowledgeItemStatus.INDEXING:
                        current.vector_store_file_id = vector_file_id
                        await session.commit()
            else:
                try:
                    status = await provider_client.get_file_status(store_id, vector_file_id)
                except Exception as error:
                    raise _ProviderFailure from error
            while status in {"in_progress", "queued"}:
                await asyncio.sleep(settings.knowledge_index_poll_interval_seconds)
                try:
                    status = await provider_client.get_file_status(store_id, vector_file_id)
                except Exception as error:
                    raise _ProviderFailure from error
        if status != "completed":
            await _fail(
                item_id,
                "KNOWLEDGE_INDEX_FAILED",
                "Провайдер не смог проиндексировать документ.",
                factory,
            )
            return
        await _mark_ready(item_id, factory)
    except TimeoutError:
        await _fail(
            item_id,
            "KNOWLEDGE_INDEX_TIMEOUT",
            "Индексация документа превысила лимит времени.",
            factory,
        )
    except _ProviderFailure as error:
        logger.exception(
            "Knowledge indexing provider failed",
            extra={"knowledge_item_id": str(item_id)},
        )
        report_exception(error, knowledge_item_id=str(item_id), event="knowledge_index_failed")
        await _fail(
            item_id,
            "KNOWLEDGE_INDEX_FAILED",
            "Индексация документа завершилась ошибкой.",
            factory,
        )


async def _fail(item_id: UUID, code: str, message: str, factory: SessionFactory) -> None:
    async with factory() as session:
        item = await KnowledgeRepository(session).get_item(item_id, lock=True)
        if item and item.status == KnowledgeItemStatus.INDEXING:
            item.status = KnowledgeItemStatus.FAILED
            item.error_code = code
            item.error_message = message
            await session.commit()


async def _mark_ready(item_id: UUID, factory: SessionFactory) -> None:
    """Finalize successful indexing; never return with an owned INDEXING row."""

    async with factory() as session:
        current = await KnowledgeRepository(session).get_item(item_id, lock=True)
        if current is None or current.status != KnowledgeItemStatus.INDEXING:
            return
        current.status = KnowledgeItemStatus.READY
        current.indexed_at = datetime.now(UTC)
        await session.commit()


async def recover_stale_indexing(session: AsyncSession) -> int:
    """Mark abandoned INDEXING items failed so they can be retried safely."""

    cutoff = datetime.now(UTC) - timedelta(seconds=settings.knowledge_index_timeout_seconds)
    result = await session.execute(
        select(KnowledgeItem)
        .where(
            KnowledgeItem.status == KnowledgeItemStatus.INDEXING,
            KnowledgeItem.updated_at < cutoff,
        )
        .with_for_update(skip_locked=True)
    )
    recovered = 0
    for item in result.scalars().all():
        item.status = KnowledgeItemStatus.FAILED
        item.error_code = "KNOWLEDGE_INDEX_STALE"
        item.error_message = "Индексация прервана после потери worker."
        recovered += 1
    if recovered:
        await session.commit()
    return recovered
