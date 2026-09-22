import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.openai_knowledge import OpenAIKnowledgeProvider
from app.models.knowledge import (
    KnowledgeStore,
    KnowledgeStoreProvider,
    KnowledgeStoreStatus,
)
from app.repositories.knowledge import KnowledgeRepository

logger = logging.getLogger(__name__)


class KnowledgeStoreService:
    def __init__(self, session: AsyncSession, provider: OpenAIKnowledgeProvider | None = None):
        self.session = session
        self.repository = KnowledgeRepository(session)
        self._provider = provider

    def provider(self) -> OpenAIKnowledgeProvider:
        if self._provider is not None:
            return self._provider
        try:
            return OpenAIKnowledgeProvider()
        except RuntimeError as exc:
            raise AppError("OPENAI_NOT_CONFIGURED", "OpenAI API не настроен.", 503) from exc

    async def get(self) -> KnowledgeStore | None:
        return await self.repository.active_store()

    async def initialize(self) -> KnowledgeStore:
        existing = await self.repository.active_store()
        if existing:
            return existing
        provider = self.provider()
        try:
            if settings.openai_vector_store_id:
                external_id, name = await provider.retrieve_store(settings.openai_vector_store_id)
            else:
                external_id, name = await provider.create_store("iTeam Knowledge Base")
        except Exception as exc:
            logger.exception("Knowledge store initialization failed")
            raise AppError(
                "KNOWLEDGE_STORE_INITIALIZATION_FAILED",
                "Не удалось инициализировать базу знаний.",
                502,
            ) from exc
        store = KnowledgeStore(
            provider=KnowledgeStoreProvider.OPENAI,
            name=name,
            external_store_id=external_id,
            status=KnowledgeStoreStatus.ACTIVE,
            is_active=True,
            metadata_={},
        )
        self.session.add(store)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            concurrent = await self.repository.active_store()
            if concurrent:
                return concurrent
            raise
        await self.session.refresh(store)
        return store
