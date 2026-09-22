from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.knowledge_pack import KnowledgePack, KnowledgePackStatus
from app.repositories.knowledge_packs import KnowledgePackRepository


class KnowledgePackService:
    def __init__(self, session: AsyncSession):
        self.repository = KnowledgePackRepository(session)

    async def get(self, pack_id: UUID) -> KnowledgePack:
        pack = await self.repository.get_by_id(pack_id)
        if not pack:
            raise AppError("KNOWLEDGE_PACK_NOT_FOUND", "Пакет знаний не найден.", 404)
        return pack

    async def list(
        self,
        *,
        campaign_id: UUID | None = None,
        task_id: UUID | None = None,
        status: KnowledgePackStatus | None = None,
    ) -> list[KnowledgePack]:
        return await self.repository.list_packs(
            campaign_id=campaign_id, task_id=task_id, status=status
        )
