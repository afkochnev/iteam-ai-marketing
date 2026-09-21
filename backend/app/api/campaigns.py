from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.campaign import CampaignStatus
from app.schemas.approval import ApprovalActionRequest, RequiredApprovalComment
from app.schemas.campaign import (
    CampaignCreate,
    CampaignListItem,
    CampaignResponse,
    CampaignUpdate,
    StrategyApprovalResponse,
    StrategyGenerationResponse,
)
from app.services.campaign_planning_service import CampaignPlanningService
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


@router.post(
    "/{campaign_id}/generate-strategy",
    response_model=StrategyGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_strategy(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> StrategyGenerationResponse:
    campaign, task, run = await CampaignPlanningService(session).generate(campaign_id)
    return StrategyGenerationResponse(
        campaign_id=campaign.id,
        planning_task_id=task.id,
        agent_run_id=run.id,
        status=campaign.status,
    )


@router.post("/{campaign_id}/approve-strategy", response_model=StrategyApprovalResponse)
async def approve_strategy(
    campaign_id: UUID,
    payload: ApprovalActionRequest,
    current_user: CurrentUser,
    session: SessionDependency,
) -> StrategyApprovalResponse:
    campaign, approval, tasks = await CampaignPlanningService(session).approve(
        campaign_id, current_user, payload.comment
    )
    current = await CampaignService(session).get_campaign(campaign.id)
    return StrategyApprovalResponse(
        campaign=CampaignResponse.model_validate(current),
        approval_id=approval.id,
        generated_task_ids=[task.id for task in tasks],
    )


@router.post("/{campaign_id}/reject-strategy", response_model=CampaignResponse)
async def reject_strategy(
    campaign_id: UUID,
    payload: RequiredApprovalComment,
    current_user: CurrentUser,
    session: SessionDependency,
) -> CampaignResponse:
    await CampaignPlanningService(session).reject(campaign_id, current_user, payload.comment)
    current = await CampaignService(session).get_campaign(campaign_id)
    return CampaignResponse.model_validate(current)


@router.post(
    "/{campaign_id}/request-strategy-revision",
    response_model=StrategyGenerationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_strategy_revision(
    campaign_id: UUID,
    payload: RequiredApprovalComment,
    current_user: CurrentUser,
    session: SessionDependency,
) -> StrategyGenerationResponse:
    campaign, task, run = await CampaignPlanningService(session).request_revision(
        campaign_id, current_user, payload.comment
    )
    return StrategyGenerationResponse(
        campaign_id=campaign.id,
        planning_task_id=task.id,
        agent_run_id=run.id,
        status=campaign.status,
    )
