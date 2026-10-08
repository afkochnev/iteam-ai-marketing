from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import CurrentUser, SessionDependency
from app.schemas.experiment import ExperimentCreate, ExperimentResponse
from app.services.experiment_service import ExperimentService

router = APIRouter(tags=["experiments"])


@router.get("/campaigns/{campaign_id}/experiments", response_model=list[ExperimentResponse])
async def list_experiments(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[ExperimentResponse]:
    return await ExperimentService(session).list_for_campaign(campaign_id)


@router.post("/campaigns/{campaign_id}/experiments", response_model=ExperimentResponse)
async def create_experiment(
    campaign_id: UUID, payload: ExperimentCreate, user: CurrentUser, session: SessionDependency
) -> ExperimentResponse:
    return await ExperimentService(session).create_from_action(campaign_id, user, payload)


@router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: UUID, _user: CurrentUser, session: SessionDependency
) -> ExperimentResponse:
    service = ExperimentService(session)
    return await service.response(await service.get(experiment_id))


@router.post("/experiments/{experiment_id}/approve", response_model=ExperimentResponse)
async def approve_experiment(
    experiment_id: UUID, user: CurrentUser, session: SessionDependency
) -> ExperimentResponse:
    return await ExperimentService(session).approve(experiment_id, user)


@router.post("/experiments/{experiment_id}/cancel", response_model=ExperimentResponse)
async def cancel_experiment(
    experiment_id: UUID, user: CurrentUser, session: SessionDependency
) -> ExperimentResponse:
    return await ExperimentService(session).cancel(experiment_id, user)
