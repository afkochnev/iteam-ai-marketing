from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeSource,
    KnowledgeSourceType,
    KnowledgeStore,
    KnowledgeStoreProvider,
)


class KnowledgeRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def active_store(self) -> KnowledgeStore | None:
        result = await self.session.execute(
            select(KnowledgeStore).where(
                KnowledgeStore.provider == KnowledgeStoreProvider.OPENAI,
                KnowledgeStore.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def default_source(self) -> KnowledgeSource | None:
        result = await self.session.execute(
            select(KnowledgeSource)
            .where(KnowledgeSource.source_type == KnowledgeSourceType.FILE_UPLOAD)
            .order_by(KnowledgeSource.created_at)
        )
        return result.scalar_one_or_none()

    async def list_sources(self) -> list[KnowledgeSource]:
        return list(
            (
                await self.session.scalars(select(KnowledgeSource).order_by(KnowledgeSource.name))
            ).all()
        )

    async def get_item(self, item_id: UUID, *, lock: bool = False) -> KnowledgeItem | None:
        query = (
            select(KnowledgeItem)
            .options(selectinload(KnowledgeItem.source))
            .where(KnowledgeItem.id == item_id)
        )
        if lock:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_items(
        self,
        *,
        status: KnowledgeItemStatus | None = None,
        source_id: UUID | None = None,
        content_type: str | None = None,
    ) -> list[KnowledgeItem]:
        query = select(KnowledgeItem).options(selectinload(KnowledgeItem.source))
        if status:
            query = query.where(KnowledgeItem.status == status)
        if source_id:
            query = query.where(KnowledgeItem.source_id == source_id)
        if content_type:
            query = query.where(KnowledgeItem.content_type == content_type)
        query = query.order_by(KnowledgeItem.created_at.desc(), KnowledgeItem.id.desc())
        return list((await self.session.scalars(query)).all())

    async def ready_by_file_ids(self, file_ids: list[str]) -> dict[str, KnowledgeItem]:
        if not file_ids:
            return {}
        items = (
            await self.session.scalars(
                select(KnowledgeItem)
                .options(selectinload(KnowledgeItem.source))
                .where(
                    KnowledgeItem.openai_file_id.in_(file_ids),
                    KnowledgeItem.status == KnowledgeItemStatus.READY,
                )
            )
        ).all()
        return {item.openai_file_id: item for item in items if item.openai_file_id}
