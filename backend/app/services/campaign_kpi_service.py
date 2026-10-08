from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_kpi import CampaignKPI
from app.schemas.campaign_kpi import KPICreate, KPIUpdate


class CampaignKPIService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def campaign(self, campaign_id: UUID, *, mutate: bool = False) -> Campaign:
        query = select(Campaign).where(Campaign.id == campaign_id)
        if mutate:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = await self.session.scalar(query)
        if row is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        if mutate and row.status == CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        return row

    async def list_for_campaign(self, campaign_id: UUID) -> list[CampaignKPI]:
        await self.campaign(campaign_id)
        return list(
            await self.session.scalars(
                select(CampaignKPI)
                .where(CampaignKPI.campaign_id == campaign_id)
                .order_by(CampaignKPI.created_at, CampaignKPI.id)
            )
        )

    async def capacity(self, campaign_id: UUID) -> None:
        count = await self.session.scalar(
            select(func.count())
            .select_from(CampaignKPI)
            .where(CampaignKPI.campaign_id == campaign_id, CampaignKPI.is_active.is_(True))
        )
        if count is not None and count >= 5:
            raise AppError("KPI_LIMIT_REACHED", "Можно настроить не более пяти активных KPI.", 409)

    async def create(self, campaign_id: UUID, data: KPICreate) -> CampaignKPI:
        await self.campaign(campaign_id, mutate=True)
        if data.is_active:
            await self.capacity(campaign_id)
        row = CampaignKPI(campaign_id=campaign_id, **data.model_dump())
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def locked(self, kpi_id: UUID) -> CampaignKPI:
        row = await self.session.get(CampaignKPI, kpi_id)
        if row is None:
            raise AppError("KPI_NOT_FOUND", "KPI не найден.", 404)
        await self.campaign(row.campaign_id, mutate=True)
        row = await self.session.scalar(
            select(CampaignKPI)
            .where(CampaignKPI.id == kpi_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise AppError("KPI_NOT_FOUND", "KPI не найден.", 404)
        return cast(CampaignKPI, row)

    async def update(self, kpi_id: UUID, data: KPIUpdate) -> CampaignKPI:
        row = await self.locked(kpi_id)
        values = {field: getattr(row, field) for field in KPICreate.model_fields}
        values.update(data.model_dump(exclude_unset=True))
        try:
            validated = KPICreate.model_validate(values)
        except ValidationError as error:
            raise AppError(
                "KPI_INVALID_CONFIGURATION", "Проверьте период и параметры KPI.", 422
            ) from error
        if validated.is_active and not row.is_active:
            await self.capacity(row.campaign_id)
        for key, value in validated.model_dump().items():
            setattr(row, key, value)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def delete(self, kpi_id: UUID) -> None:
        # No KPI-dependent provenance exists in PR30; observations are never deleted.
        row = await self.locked(kpi_id)
        await self.session.delete(row)
        await self.session.commit()
