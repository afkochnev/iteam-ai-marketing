from uuid import UUID

from fastapi import APIRouter, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.errors import AppError
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.schemas.feedback import FeedbackAnalysisResponse, FeedbackCreate, FeedbackResponse
from app.services.feedback_service import FeedbackService

router = APIRouter(tags=["feedback"])


@router.post(
    "/campaigns/{campaign_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_feedback(
    campaign_id: UUID,
    payload: FeedbackCreate,
    user: CurrentUser,
    session: SessionDependency,
) -> FeedbackResponse:
    row = await FeedbackService(session).create_feedback(campaign_id, user, payload.model_dump())
    return FeedbackResponse.model_validate(row)


@router.get("/campaigns/{campaign_id}/feedback", response_model=list[FeedbackResponse])
async def list_feedback(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[FeedbackResponse]:
    return [
        FeedbackResponse.model_validate(row)
        for row in await FeedbackService(session).list_feedback(campaign_id)
    ]


@router.post(
    "/campaigns/{campaign_id}/performance-analysis",
    response_model=FeedbackAnalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/campaigns/{campaign_id}/feedback-analysis",
    response_model=FeedbackAnalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_feedback_analysis(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> FeedbackAnalysisResponse:
    row = await FeedbackService(session).prepare_analysis(
        campaign_id, requested_by_user_id=_user.id
    )
    assert row is not None
    return FeedbackAnalysisResponse.model_validate(row)


@router.get(
    "/campaigns/{campaign_id}/performance-analysis", response_model=list[FeedbackAnalysisResponse]
)
@router.get(
    "/campaigns/{campaign_id}/feedback-analysis",
    response_model=list[FeedbackAnalysisResponse],
)
async def list_feedback_analysis(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[FeedbackAnalysisResponse]:
    return [
        FeedbackAnalysisResponse.model_validate(row)
        for row in await FeedbackService(session).list_analyses(campaign_id)
    ]


@router.get("/feedback-analysis/{analysis_id}", response_model=FeedbackAnalysisResponse)
async def get_feedback_analysis(
    analysis_id: UUID, _user: CurrentUser, session: SessionDependency
) -> FeedbackAnalysisResponse:
    row = await session.get(MarketingFeedbackAnalysis, analysis_id)
    if row is None:
        raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
    return FeedbackAnalysisResponse.model_validate(row)


async def _review(
    analysis_id: UUID,
    user: CurrentUser,
    session: SessionDependency,
    decision: FeedbackAnalysisStatus,
) -> FeedbackAnalysisResponse:
    row = await FeedbackService(session).review(analysis_id, user, decision)
    return FeedbackAnalysisResponse.model_validate(row)


@router.post("/feedback-analysis/{analysis_id}/accept", response_model=FeedbackAnalysisResponse)
async def accept_feedback_analysis(
    analysis_id: UUID, user: CurrentUser, session: SessionDependency
) -> FeedbackAnalysisResponse:
    return await _review(analysis_id, user, session, FeedbackAnalysisStatus.ACCEPTED)


@router.post("/feedback-analysis/{analysis_id}/reject", response_model=FeedbackAnalysisResponse)
async def reject_feedback_analysis(
    analysis_id: UUID, user: CurrentUser, session: SessionDependency
) -> FeedbackAnalysisResponse:
    return await _review(analysis_id, user, session, FeedbackAnalysisStatus.REJECTED)


@router.post(
    "/performance-analysis/{analysis_id}/retry",
    response_model=FeedbackAnalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@router.post(
    "/feedback-analysis/{analysis_id}/retry",
    response_model=FeedbackAnalysisResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_feedback_analysis(
    analysis_id: UUID, _user: CurrentUser, session: SessionDependency
) -> FeedbackAnalysisResponse:
    return FeedbackAnalysisResponse.model_validate(
        await FeedbackService(session).retry_analysis(analysis_id)
    )
