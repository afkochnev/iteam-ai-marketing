from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.content import ContentItem
from app.repositories.content import ContentRepository


class ContentService:
    def __init__(self, session: AsyncSession):
        self.repository = ContentRepository(session)

    async def list(self, **filters: Any) -> list[ContentItem]:
        return await self.repository.list(**filters)

    async def get(self, content_id: UUID) -> ContentItem:
        item = await self.repository.get(content_id)
        if item is None:
            raise AppError("CONTENT_NOT_FOUND", "Материал не найден.", 404)
        return item
