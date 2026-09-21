from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.campaign import CampaignStatus
from app.schemas.campaign import CampaignCreate, CampaignListItem, CampaignResponse, CampaignUpdate
from app.services.campaign_service import CampaignService

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


@router.get("", response_model=list[CampaignListItem])
async def list_campaigns(
    _current_user: CurrentUser,
    session: SessionDependency,
    campaign_status: Annotated[CampaignStatus | None, Query(alias="status")] = None,
) -> list[CampaignListItem]:
    campaigns = await CampaignService(session).list_campaigns(campaign_status)
    return [CampaignListItem.model_validate(campaign) for campaign in campaigns]


@router.post("", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    current_user: CurrentUser,
    session: SessionDependency,
) -> CampaignResponse:
    campaign = await CampaignService(session).create_campaign(payload, current_user)
    return CampaignResponse.model_validate(campaign)


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: UUID,
    _current_user: CurrentUser,
    session: SessionDependency,
) -> CampaignResponse:
    campaign = await CampaignService(session).get_campaign(campaign_id)
    return CampaignResponse.model_validate(campaign)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: UUID,
    payload: CampaignUpdate,
    _current_user: CurrentUser,
    session: SessionDependency,
) -> CampaignResponse:
    campaign = await CampaignService(session).update_campaign(campaign_id, payload)
    return CampaignResponse.model_validate(campaign)


@router.post("/{campaign_id}/archive", response_model=CampaignResponse)
async def archive_campaign(
    campaign_id: UUID,
    _current_user: CurrentUser,
    session: SessionDependency,
) -> CampaignResponse:
    campaign = await CampaignService(session).archive_campaign(campaign_id)
    return CampaignResponse.model_validate(campaign)
