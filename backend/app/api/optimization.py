from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.optimization import OptimizationActionStatus
from app.schemas.optimization import OptimizationActionResponse, OptimizationProposalResponse
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
        OptimizationProposalResponse.model_validate(row)
        for row in await OptimizationProposalService(session).list(campaign_id)
    ]


@router.get("/optimization-proposals/{proposal_id}", response_model=OptimizationProposalResponse)
async def get_proposal(
    proposal_id: UUID, _user: CurrentUser, session: SessionDependency
) -> OptimizationProposalResponse:
    return OptimizationProposalResponse.model_validate(
        await OptimizationProposalService(session).get(proposal_id)
    )


@router.post("/optimization-actions/{action_id}/approve", response_model=OptimizationActionResponse)
async def approve(
    action_id: UUID, user: CurrentUser, session: SessionDependency
) -> OptimizationActionResponse:
    return OptimizationActionResponse.model_validate(
        await OptimizationProposalService(session).decide(
            action_id, user, OptimizationActionStatus.APPROVED
        )
    )


@router.post("/optimization-actions/{action_id}/reject", response_model=OptimizationActionResponse)
async def reject(
    action_id: UUID, user: CurrentUser, session: SessionDependency
) -> OptimizationActionResponse:
    return OptimizationActionResponse.model_validate(
        await OptimizationProposalService(session).decide(
            action_id, user, OptimizationActionStatus.REJECTED
        )
    )
