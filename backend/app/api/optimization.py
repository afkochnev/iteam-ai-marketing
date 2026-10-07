from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.optimization import CampaignOptimizationProposal, OptimizationActionStatus
from app.schemas.optimization import (
    OptimizationActionApplyRequest,
    OptimizationActionApplyResponse,
    OptimizationActionResponse,
    OptimizationProposalResponse,
)
from app.services.optimization_apply_service import OptimizationApplyService
from app.services.optimization_proposal_service import OptimizationProposalService

router = APIRouter(tags=["optimization"])


@router.get(
    "/campaigns/{campaign_id}/optimization-proposals",
    response_model=list[OptimizationProposalResponse],
)
async def list_proposals(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[OptimizationProposalResponse]:
    return [
        await proposal_response(row, session)
        for row in await OptimizationProposalService(session).list(campaign_id)
    ]


@router.get("/optimization-proposals/{proposal_id}", response_model=OptimizationProposalResponse)
async def get_proposal(
    proposal_id: UUID, _user: CurrentUser, session: SessionDependency
) -> OptimizationProposalResponse:
    return await proposal_response(
        await OptimizationProposalService(session).get(proposal_id), session
    )


@router.post("/optimization-actions/{action_id}/approve", response_model=OptimizationActionResponse)
async def approve(
    action_id: UUID, user: CurrentUser, session: SessionDependency
) -> OptimizationActionResponse:
    return await OptimizationApplyService(session).action_response(
        await OptimizationProposalService(session).decide(
            action_id, user, OptimizationActionStatus.APPROVED
        )
    )


@router.post("/optimization-actions/{action_id}/reject", response_model=OptimizationActionResponse)
async def reject(
    action_id: UUID, user: CurrentUser, session: SessionDependency
) -> OptimizationActionResponse:
    return await OptimizationApplyService(session).action_response(
        await OptimizationProposalService(session).decide(
            action_id, user, OptimizationActionStatus.REJECTED
        )
    )


async def proposal_response(
    row: CampaignOptimizationProposal, session: SessionDependency
) -> OptimizationProposalResponse:
    response = OptimizationProposalResponse.model_validate(row)
    response.actions = [
        await OptimizationApplyService(session).action_response(action) for action in row.actions
    ]
    return response


@router.post(
    "/optimization-actions/{action_id}/apply", response_model=OptimizationActionApplyResponse
)
async def apply_action(
    action_id: UUID,
    payload: OptimizationActionApplyRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> OptimizationActionApplyResponse:
    return await OptimizationProposalService(session).apply_action(action_id, user, payload)
