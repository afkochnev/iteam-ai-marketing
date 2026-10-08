from uuid import UUID

from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser, SessionDependency
from app.schemas.campaign_kpi import KPICreate, KPIResponse, KPIUpdate
from app.services.campaign_kpi_service import CampaignKPIService

router = APIRouter(tags=["campaign-kpis"])


@router.get("/campaigns/{campaign_id}/kpis", response_model=list[KPIResponse])
async def list_kpis(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[KPIResponse]:
    return [
        KPIResponse.model_validate(row)
        for row in await CampaignKPIService(session).list_for_campaign(campaign_id)
    ]


@router.post("/campaigns/{campaign_id}/kpis", response_model=KPIResponse, status_code=201)
async def create_kpi(
    campaign_id: UUID, data: KPICreate, _user: CurrentUser, session: SessionDependency
) -> KPIResponse:
    return KPIResponse.model_validate(await CampaignKPIService(session).create(campaign_id, data))


@router.patch("/campaign-kpis/{kpi_id}", response_model=KPIResponse)
async def update_kpi(
    kpi_id: UUID, data: KPIUpdate, _user: CurrentUser, session: SessionDependency
) -> KPIResponse:
    return KPIResponse.model_validate(await CampaignKPIService(session).update(kpi_id, data))


@router.delete("/campaign-kpis/{kpi_id}", status_code=204)
async def delete_kpi(kpi_id: UUID, _user: CurrentUser, session: SessionDependency) -> Response:
    await CampaignKPIService(session).delete(kpi_id)
    return Response(status_code=204)
