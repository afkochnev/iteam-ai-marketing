from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.content import ContentItem, ContentVersion

class ContentRepository:
    def __init__(self, session: AsyncSession): self.session = session
    async def list(self, **filters: object) -> list[ContentItem]:
        stmt = select(ContentItem).options(selectinload(ContentItem.versions).selectinload(ContentVersion.sources)).order_by(ContentItem.created_at.desc(), ContentItem.id.desc())
        for field, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(ContentItem, field) == value)
        return list((await self.session.execute(stmt)).scalars().unique().all())
    async def get(self, content_id: UUID) -> ContentItem | None:
        stmt = select(ContentItem).options(selectinload(ContentItem.versions).selectinload(ContentVersion.sources)).where(ContentItem.id == content_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()
