from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.knowledge_pack import KnowledgePack, KnowledgePackStatus


class KnowledgePackRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, pack_id: UUID) -> KnowledgePack | None:
        return (
            await self.session.execute(
                select(KnowledgePack)
                .options(selectinload(KnowledgePack.items))
                .where(KnowledgePack.id == pack_id)
            )
        ).scalar_one_or_none()

    async def get_by_run(self, run_id: UUID) -> KnowledgePack | None:
        return (
            await self.session.execute(
                select(KnowledgePack)
                .options(selectinload(KnowledgePack.items))
                .where(KnowledgePack.agent_run_id == run_id)
            )
        ).scalar_one_or_none()

    async def list_packs(
        self,
        *,
        campaign_id: UUID | None = None,
        task_id: UUID | None = None,
        status: KnowledgePackStatus | None = None,
    ) -> list[KnowledgePack]:
        query = select(KnowledgePack).options(selectinload(KnowledgePack.items))
        if campaign_id:
            query = query.where(KnowledgePack.campaign_id == campaign_id)
        if task_id:
            query = query.where(KnowledgePack.task_id == task_id)
        if status:
            query = query.where(KnowledgePack.status == status)
        query = query.order_by(KnowledgePack.created_at.desc(), KnowledgePack.id.desc())
        return list((await self.session.scalars(query)).all())
