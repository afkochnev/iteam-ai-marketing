from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.campaign import Campaign, CampaignStatus


class CampaignRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_campaigns(self, status: CampaignStatus | None = None) -> list[Campaign]:
        statement = select(Campaign)
        if status is None:
            statement = statement.where(Campaign.status != CampaignStatus.ARCHIVED)
        else:
            statement = statement.where(Campaign.status == status)
        statement = statement.order_by(Campaign.created_at.desc(), Campaign.id.desc())
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def get_by_id(self, campaign_id: UUID) -> Campaign | None:
        result = await self.session.execute(
            select(Campaign).options(joinedload(Campaign.creator)).where(Campaign.id == campaign_id)
        )
        return result.scalar_one_or_none()

    async def create(self, values: Mapping[str, Any], created_by: UUID) -> Campaign:
        campaign = Campaign(
            **values,
            created_by=created_by,
            status=CampaignStatus.DRAFT,
            strategy=None,
        )
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def update(self, campaign: Campaign, changes: Mapping[str, Any]) -> Campaign:
        for field, value in changes.items():
            setattr(campaign, field, value)
        await self.session.flush()
        return campaign

    async def archive(self, campaign: Campaign) -> Campaign:
        campaign.status = CampaignStatus.ARCHIVED
        await self.session.flush()
        return campaign
