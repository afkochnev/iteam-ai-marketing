import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.openai_knowledge import OpenAIKnowledgeProvider
from app.models.knowledge import KnowledgeItem, KnowledgeItemStatus
from app.repositories.knowledge import KnowledgeRepository
from app.services.knowledge_store_service import KnowledgeStoreService

logger = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
SUPPORTED_MIME_TYPES = {
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".txt": {"text/plain", "application/octet-stream"},
    ".md": {"text/markdown", "text/plain", "application/octet-stream"},
}
RETRYABLE_INDEX_ERROR_CODES = frozenset(
    {
        "KNOWLEDGE_INDEX_STALE",
        "KNOWLEDGE_INDEX_TIMEOUT",
        "KNOWLEDGE_INDEX_FAILED",
        "KNOWLEDGE_INDEX_ENQUEUE_FAILED",
        "OPENAI_FILE_UPLOAD_FAILED",
    }
)


def safe_provider_message(_exc: Exception) -> str:
    return "Операция с провайдером базы знаний завершилась ошибкой."


class KnowledgeService:
    def __init__(self, session: AsyncSession, provider: OpenAIKnowledgeProvider | None = None):
        self.session = session
        self.repository = KnowledgeRepository(session)
        self._provider = provider

    def provider(self) -> OpenAIKnowledgeProvider:
        return KnowledgeStoreService(self.session, self._provider).provider()

    async def list_items(self, **filters: object) -> list[KnowledgeItem]:
        return await self.repository.list_items(**filters)  # type: ignore[arg-type]

    async def get_item(self, item_id: UUID) -> KnowledgeItem:
        item = await self.repository.get_item(item_id)
        if not item:
            raise AppError("KNOWLEDGE_ITEM_NOT_FOUND", "Документ не найден.", 404)
        return item

    async def upload(
        self,
        *,
        filename: str,
        content: bytes,
        mime_type: str | None,
        title: str | None,
        author: str | None,
        created_by: UUID,
    ) -> KnowledgeItem:
        clean_name = Path(filename).name
        extension = Path(clean_name).suffix.lower()
        if not clean_name or clean_name in {".", ".."} or extension not in SUPPORTED_EXTENSIONS:
            raise AppError("UNSUPPORTED_KNOWLEDGE_FILE", "Поддерживаются PDF, DOCX, TXT и MD.", 422)
        if mime_type and mime_type.lower() not in SUPPORTED_MIME_TYPES[extension]:
            raise AppError(
                "UNSUPPORTED_KNOWLEDGE_MIME",
                "Тип файла не соответствует расширению.",
                422,
            )
        if not content:
            raise AppError("EMPTY_KNOWLEDGE_FILE", "Файл не может быть пустым.", 422)
        if len(content) > settings.max_upload_size_mb * 1024 * 1024:
            raise AppError("KNOWLEDGE_FILE_TOO_LARGE", "Файл превышает допустимый размер.", 413)
        source = await self.repository.default_source()
        if source is None:
            raise AppError("KNOWLEDGE_SOURCE_NOT_CONFIGURED", "Источник загрузок не настроен.", 409)
        item = KnowledgeItem(
            source_id=source.id,
            title=(title or Path(clean_name).stem).strip()[:255],
            author=author.strip()[:255] if author and author.strip() else None,
            content_type=extension.removeprefix("."),
            original_filename=clean_name[:255],
            mime_type=mime_type,
            file_size_bytes=len(content),
            source_content=content,
            status=KnowledgeItemStatus.UPLOADING,
            metadata_={},
            created_by=created_by,
        )
        self.session.add(item)
        await self.session.commit()
        await self.session.refresh(item)
        try:
            item.openai_file_id = await self.provider().upload_file(clean_name, content)
            item.status = KnowledgeItemStatus.INDEXING
            await self.session.commit()
        except Exception as exc:
            logger.exception(
                "Knowledge file upload failed",
                extra={"knowledge_item_id": str(item.id)},
            )
            item.status = KnowledgeItemStatus.FAILED
            item.error_code = "OPENAI_FILE_UPLOAD_FAILED"
            item.error_message = safe_provider_message(exc)
            await self.session.commit()
            raise AppError("OPENAI_FILE_UPLOAD_FAILED", item.error_message, 502) from exc
        return await self.get_item(item.id)

    async def enqueue_indexing(self, item: KnowledgeItem) -> KnowledgeItem:
        try:
            from app.workers.knowledge_worker import index_knowledge_item

            index_knowledge_item.apply_async(args=[str(item.id)], queue="ai")
        except Exception as exc:
            logger.exception("Knowledge indexing enqueue failed")
            item.status = KnowledgeItemStatus.FAILED
            item.error_code = "KNOWLEDGE_INDEX_ENQUEUE_FAILED"
            item.error_message = "Не удалось поставить документ на индексацию."
            await self.session.commit()
            raise AppError(item.error_code, item.error_message, 503) from exc
        return item

    async def retry(self, item_id: UUID) -> KnowledgeItem:
        item = await self.repository.get_item(item_id, lock=True)
        if (
            item is None
            or item.status is not KnowledgeItemStatus.FAILED
            or item.error_code not in RETRYABLE_INDEX_ERROR_CODES
        ):
            raise AppError(
                "KNOWLEDGE_ITEM_NOT_RETRYABLE",
                "Документ нельзя повторно индексировать.",
                409,
            )
        item.status = KnowledgeItemStatus.INDEXING
        item.error_code = None
        item.error_message = None
        await self.session.commit()
        item = await self.get_item(item.id)
        await self.enqueue_indexing(item)
        # Server-managed timestamps (notably updated_at) may be expired after
        # the state transition commit. Reload the complete ORM row before the
        # async endpoint hands it to Pydantic's from_attributes serializer.
        return await self.get_item(item.id)

    async def archive(self, item_id: UUID) -> KnowledgeItem:
        item = await self.get_item(item_id)
        if item.status is KnowledgeItemStatus.ARCHIVED:
            return item
        store = await self.repository.active_store()
        if store and item.vector_store_file_id:
            try:
                await self.provider().detach_file(
                    store.external_store_id, item.vector_store_file_id
                )
            except Exception as exc:
                logger.exception("Knowledge file detach failed")
                raise AppError("KNOWLEDGE_ARCHIVE_FAILED", safe_provider_message(exc), 502) from exc
        item.status = KnowledgeItemStatus.ARCHIVED
        item.archived_at = datetime.now(UTC)
        await self.session.commit()
        return await self.get_item(item.id)
