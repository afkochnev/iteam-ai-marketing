import hashlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.integrations.openai_knowledge import OpenAIKnowledgeProvider
from app.repositories.knowledge import KnowledgeRepository
from app.schemas.knowledge import KnowledgeSearchResponse, KnowledgeSearchResult
from app.services.knowledge_store_service import KnowledgeStoreService

logger = logging.getLogger(__name__)


def build_result_key(knowledge_item_id: object, file_id: str, excerpt: str) -> str:
    return hashlib.sha256(f"{knowledge_item_id}\0{file_id}\0{excerpt}".encode()).hexdigest()


class KnowledgeSearchService:
    def __init__(self, session: AsyncSession, provider: OpenAIKnowledgeProvider | None = None):
        self.session = session
        self.repository = KnowledgeRepository(session)
        self._provider = provider

    async def search(self, query: str, max_results: int = 10) -> KnowledgeSearchResponse:
        query = query.strip()
        if not query:
            raise AppError("INVALID_KNOWLEDGE_QUERY", "Поисковый запрос не может быть пустым.", 422)
        if not 1 <= max_results <= 20:
            raise AppError(
                "INVALID_KNOWLEDGE_RESULT_LIMIT", "Допустимо от 1 до 20 результатов.", 422
            )
        store = await self.repository.active_store()
        if not store:
            raise AppError(
                "KNOWLEDGE_STORE_NOT_INITIALIZED", "База знаний не инициализирована.", 409
            )
        provider = KnowledgeStoreService(self.session, self._provider).provider()
        try:
            provider_results = await provider.search(store.external_store_id, query, max_results)
        except Exception as exc:
            logger.exception("Knowledge search failed")
            raise AppError(
                "KNOWLEDGE_SEARCH_FAILED", "Поиск по базе знаний завершился ошибкой.", 502
            ) from exc
        mapped = await self.repository.ready_by_file_ids(
            [item.file_id for item in provider_results]
        )
        results: list[KnowledgeSearchResult] = []
        for result in provider_results:
            item = mapped.get(result.file_id)
            if not item:
                logger.warning(
                    "Ignoring unmapped knowledge search result", extra={"file_id": result.file_id}
                )
                continue
            results.append(
                KnowledgeSearchResult(
                    result_key=build_result_key(item.id, result.file_id, result.excerpt),
                    knowledge_item_id=item.id,
                    source_id=item.source_id,
                    source_title=item.source.name,
                    filename=result.filename or item.original_filename or item.title,
                    file_id=result.file_id,
                    excerpt=result.excerpt,
                    score=result.score,
                    metadata=result.attributes,
                )
            )
        return KnowledgeSearchResponse(query=query, result_count=len(results), results=results)
