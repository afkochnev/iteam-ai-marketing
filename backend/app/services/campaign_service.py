from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.campaign import Campaign, CampaignStatus
from app.models.user import User
from app.repositories.campaigns import CampaignRepository
from app.schemas.campaign import CampaignCreate, CampaignUpdate


class CampaignService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = CampaignRepository(session)

    async def list_campaigns(self, status: CampaignStatus | None = None) -> list[Campaign]:
        return await self.repository.list_campaigns(status)

    async def get_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = await self.repository.get_by_id(campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        return campaign

    async def create_campaign(self, payload: CampaignCreate, creator: User) -> Campaign:
        campaign = await self.repository.create(payload.model_dump(), creator.id)
        await self.session.commit()
        return await self.get_campaign(campaign.id)

    async def update_campaign(self, campaign_id: UUID, payload: CampaignUpdate) -> Campaign:
        campaign = await self.get_campaign(campaign_id)
        if campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED",
                "Архивную кампанию нельзя редактировать.",
                409,
            )
        changes = payload.model_dump(exclude_unset=True)
        start_date = changes.get("start_date", campaign.start_date)
        end_date = changes.get("end_date", campaign.end_date)
        self._validate_date_range(start_date, end_date)
        await self.repository.update(campaign, changes)
        await self.session.commit()
        return await self.get_campaign(campaign.id)

    async def archive_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = await self.get_campaign(campaign_id)
        if campaign.status is not CampaignStatus.ARCHIVED:
            await self.repository.archive(campaign)
            await self.session.commit()
        return await self.get_campaign(campaign.id)

    @staticmethod
    def _validate_date_range(start_date: date | None, end_date: date | None) -> None:
        if start_date and end_date and start_date > end_date:
            raise AppError(
                "INVALID_CAMPAIGN_DATE_RANGE",
                "Дата окончания не может быть раньше даты начала.",
                422,
            )
