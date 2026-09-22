import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.knowledge import KnowledgeItemStatus
from app.repositories.knowledge import KnowledgeRepository
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="index_knowledge_item")  # type: ignore[untyped-decorator]
def index_knowledge_item(knowledge_item_id: str) -> None:
    asyncio.run(_index(UUID(knowledge_item_id)))


async def _index(item_id: UUID) -> None:
    try:
        async with async_session_factory() as session:
            repository = KnowledgeRepository(session)
            item = await repository.get_item(item_id, lock=True)
            if item is None or item.status is not KnowledgeItemStatus.INDEXING:
                return
            if not item.openai_file_id:
                await _fail(item_id, "KNOWLEDGE_FILE_ID_MISSING", "OpenAI File ID отсутствует.")
                return
            store = await repository.active_store()
            if store is None:
                await _fail(
                    item_id, "KNOWLEDGE_STORE_NOT_INITIALIZED", "База знаний не инициализирована."
                )
                return
            store_id = store.external_store_id
            file_id = item.openai_file_id
            vector_file_id = item.vector_store_file_id
            source_id = item.source_id
        try:
            from app.integrations.openai_knowledge import OpenAIKnowledgeProvider

            provider_client = OpenAIKnowledgeProvider()
            if vector_file_id is None:
                vector_file_id, status = await provider_client.attach_file(
                    store_id,
                    file_id,
                    {
                        "knowledge_item_id": str(item_id),
                        "source_id": str(source_id),
                        "content_type": item.content_type,
                        "is_active": True,
                    },
                )
                async with async_session_factory() as session:
                    current = await KnowledgeRepository(session).get_item(item_id, lock=True)
                    if current and current.status is KnowledgeItemStatus.INDEXING:
                        current.vector_store_file_id = vector_file_id
                        await session.commit()
            else:
                status = await provider_client.get_file_status(store_id, vector_file_id)
            deadline = asyncio.get_running_loop().time() + settings.knowledge_index_timeout_seconds
            while status in {"in_progress", "queued"}:
                if asyncio.get_running_loop().time() >= deadline:
                    await _fail(
                        item_id,
                        "KNOWLEDGE_INDEX_TIMEOUT",
                        "Индексация документа превысила лимит времени.",
                    )
                    return
                await asyncio.sleep(settings.knowledge_index_poll_interval_seconds)
                status = await provider_client.get_file_status(store_id, vector_file_id)
            if status != "completed":
                await _fail(
                    item_id,
                    "KNOWLEDGE_INDEX_FAILED",
                    "Провайдер не смог проиндексировать документ.",
                )
                return
            async with async_session_factory() as session:
                current = await KnowledgeRepository(session).get_item(item_id, lock=True)
                if current and current.status is KnowledgeItemStatus.INDEXING:
                    current.status = KnowledgeItemStatus.READY
                    current.indexed_at = datetime.now(UTC)
                    await session.commit()
        except Exception:
            logger.exception("Knowledge indexing failed", extra={"knowledge_item_id": str(item_id)})
            await _fail(
                item_id, "KNOWLEDGE_INDEX_FAILED", "Индексация документа завершилась ошибкой."
            )
    except Exception:
        logger.exception(
            "Knowledge worker failed safely", extra={"knowledge_item_id": str(item_id)}
        )


async def _fail(item_id: UUID, code: str, message: str) -> None:
    async with async_session_factory() as session:
        item = await KnowledgeRepository(session).get_item(item_id, lock=True)
        if item and item.status is KnowledgeItemStatus.INDEXING:
            item.status = KnowledgeItemStatus.FAILED
            item.error_code = code
            item.error_message = message
            await session.commit()
