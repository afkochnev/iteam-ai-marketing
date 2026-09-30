from uuid import UUID

from fastapi import APIRouter, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.publication_plan import PublicationPlan, PublicationPlanStatus
from app.schemas.publication_plan import (
    PlanItemInput,
    PublicationPlanCreate,
    PublicationPlanGenerateRequest,
    PublicationPlanItemUpdate,
    PublicationPlanReorder,
    PublicationPlanResponse,
)
from app.services.publication_plan_service import PublicationPlanService

router = APIRouter(tags=["publication-plans"])


async def _response(
    service: PublicationPlanService, plan: PublicationPlan
) -> PublicationPlanResponse:
    response = PublicationPlanResponse.model_validate(plan)
    warnings = await service.collision_warnings(plan)
    for item in response.items:
        item.near_publication_warnings = warnings.get(item.id, [])
    return response


@router.get(
    "/campaigns/{campaign_id}/publication-plans", response_model=list[PublicationPlanResponse]
)
async def list_plans(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[PublicationPlanResponse]:
    service = PublicationPlanService(session)
    return [await _response(service, row) for row in await service.list_plans(campaign_id)]


@router.post(
    "/campaigns/{campaign_id}/publication-plans",
    response_model=PublicationPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_plan(
    campaign_id: UUID, payload: PublicationPlanCreate, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.create_plan(campaign_id, user, payload))


@router.post(
    "/campaigns/{campaign_id}/publication-plans/generate",
    response_model=PublicationPlanResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_plan(
    campaign_id: UUID,
    payload: PublicationPlanGenerateRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.queue_generation(campaign_id, user, payload))


@router.get("/publication-plans/{plan_id}", response_model=PublicationPlanResponse)
async def get_plan(
    plan_id: UUID, _user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.get_plan(plan_id))


@router.post("/publication-plans/{plan_id}/items", response_model=PublicationPlanResponse)
async def add_plan_item(
    plan_id: UUID, payload: PlanItemInput, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.add_item(plan_id, payload, user))


@router.patch(
    "/publication-plans/{plan_id}/items/{item_id}", response_model=PublicationPlanResponse
)
async def update_plan_item(
    plan_id: UUID,
    item_id: UUID,
    payload: PublicationPlanItemUpdate,
    user: CurrentUser,
    session: SessionDependency,
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.update_item(plan_id, item_id, payload, user))


@router.delete(
    "/publication-plans/{plan_id}/items/{item_id}", response_model=PublicationPlanResponse
)
async def remove_plan_item(
    plan_id: UUID, item_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.remove_item(plan_id, item_id, user))


@router.post("/publication-plans/{plan_id}/reorder", response_model=PublicationPlanResponse)
async def reorder_plan(
    plan_id: UUID,
    payload: PublicationPlanReorder,
    user: CurrentUser,
    session: SessionDependency,
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(service, await service.reorder(plan_id, payload.item_ids, user))


@router.post("/publication-plans/{plan_id}/submit", response_model=PublicationPlanResponse)
async def submit_plan(
    plan_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(
        service, await service.transition(plan_id, user, PublicationPlanStatus.WAITING_APPROVAL)
    )


@router.post("/publication-plans/{plan_id}/approve", response_model=PublicationPlanResponse)
async def approve_plan(
    plan_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(
        service, await service.transition(plan_id, user, PublicationPlanStatus.APPROVED)
    )


@router.post("/publication-plans/{plan_id}/reject", response_model=PublicationPlanResponse)
async def reject_plan(
    plan_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(
        service, await service.transition(plan_id, user, PublicationPlanStatus.REJECTED)
    )


@router.post("/publication-plans/{plan_id}/revise", response_model=PublicationPlanResponse)
async def revise_plan(
    plan_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationPlanResponse:
    service = PublicationPlanService(session)
    return await _response(
        service, await service.transition(plan_id, user, PublicationPlanStatus.DRAFT)
    )
