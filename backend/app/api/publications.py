from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.config import settings
from app.core.errors import AppError
from app.models.publication import PublicationStatus
from app.schemas.publication import (
    PublicationCalendarItem,
    PublicationCreate,
    PublicationReconcileNotPublishedRequest,
    PublicationReconcilePublishedRequest,
    PublicationResponse,
    PublicationScheduleRequest,
)
from app.services.activity_log_service import ActivityLogService
from app.services.publication_service import PublicationService

router = APIRouter(prefix="/publications", tags=["publications"])


@router.post("", response_model=PublicationResponse, status_code=status.HTTP_201_CREATED)
async def create_publication(
    payload: PublicationCreate, user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    return await PublicationService(session).create(payload, user)


@router.get("/campaign/{campaign_id}", response_model=list[PublicationResponse])
async def list_campaign_publications(
    campaign_id: UUID, _user: CurrentUser, session: SessionDependency
) -> list[PublicationResponse]:
    return await PublicationService(session).list_campaign(campaign_id)


@router.get("/campaign/{campaign_id}/calendar", response_model=list[PublicationCalendarItem])
async def campaign_publication_calendar(
    campaign_id: UUID,
    _user: CurrentUser,
    session: SessionDependency,
    from_at: Annotated[datetime, Query(alias="from")],
    to_at: Annotated[datetime, Query(alias="to")],
) -> list[PublicationCalendarItem]:
    return await PublicationService(session).calendar(campaign_id, from_at, to_at)


@router.get("/{publication_id}", response_model=PublicationResponse)
async def get_publication(
    publication_id: UUID, _user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    return await PublicationService(session).get(publication_id)


@router.post("/{publication_id}/approve", response_model=PublicationResponse)
async def approve_publication(
    publication_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    return await PublicationService(session).approve(publication_id, user)


@router.post("/{publication_id}/schedule", response_model=PublicationResponse)
async def schedule_publication(
    publication_id: UUID,
    payload: PublicationScheduleRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> PublicationResponse:
    return await PublicationService(session).schedule(publication_id, payload.scheduled_at, user)


@router.post("/{publication_id}/cancel", response_model=PublicationResponse)
async def cancel_publication(
    publication_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    return await PublicationService(session).cancel(publication_id, user)


@router.post(
    "/{publication_id}/publish-now",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def publish_now(
    publication_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    publication = await PublicationService(session).claim_for_publish(publication_id, user)
    if publication.channel.value == "VK":
        from app.workers.vk_worker import publish_vk_publication

        publish_vk_publication.delay(str(publication.id))
    else:
        from app.workers.telegram_worker import publish_telegram_publication

        publish_telegram_publication.delay(str(publication.id))
    return publication


@router.post(
    "/{publication_id}/retry",
    response_model=PublicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_publication(
    publication_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    service = PublicationService(session)
    publication = await service._locked(publication_id)
    if publication.status is not PublicationStatus.FAILED or publication.failure_code not in {
        "TELEGRAM_RATE_LIMIT",
        "TELEGRAM_PROVIDER_TIMEOUT",
        "TELEGRAM_PROVIDER_ERROR",
        "VK_RATE_LIMIT",
        "VK_PROVIDER_ERROR",
        "PUBLICATION_RECONCILED_NOT_PUBLISHED",
    }:
        raise AppError(
            "PUBLICATION_NOT_RETRYABLE", "Публикацию нельзя повторить автоматически.", 409
        )
    if publication.retry_count >= settings.publication_max_retries:
        raise AppError("PUBLICATION_RETRY_EXHAUSTED", "Лимит повторных публикаций исчерпан.", 409)
    if (publication.channel.value == "TELEGRAM" and not settings.telegram_publishing_enabled) or (
        publication.channel.value == "VK" and not settings.vk_publishing_enabled
    ):
        raise AppError("PUBLICATION_PROVIDER_DISABLED", "Провайдер публикации отключён.", 409)
    previous_failure = publication.failure_code
    publication.status = PublicationStatus.APPROVED
    publication.execution_token = None
    publication.failure_code = None
    publication.failure_message = None
    await ActivityLogService(session).record(
        "PUBLICATION_RETRY_SCHEDULED",
        operation_key=f"publication-retry:{publication.id}:{publication.retry_count + 1}",
        campaign_id=publication.campaign_id,
        user_id=user.id,
        content_item_id=publication.content_item_id,
        metadata={"publication_id": str(publication.id), "failure_code": previous_failure},
    )
    await session.commit()
    claimed = await service.claim_for_publish(publication_id, user)
    if claimed.channel.value == "VK":
        from app.workers.vk_worker import publish_vk_publication

        publish_vk_publication.delay(str(claimed.id))
    else:
        from app.workers.telegram_worker import publish_telegram_publication

        publish_telegram_publication.delay(str(claimed.id))
    return claimed


@router.post(
    "/{publication_id}/reconcile/published",
    response_model=PublicationResponse,
)
async def reconcile_published(
    publication_id: UUID,
    payload: PublicationReconcilePublishedRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> PublicationResponse:
    return await PublicationService(session).reconcile_published(
        publication_id,
        user,
        external_id=payload.external_id,
        external_url=payload.external_url,
        published_at=payload.published_at,
        note=payload.note,
    )


@router.post(
    "/{publication_id}/reconcile/not-published",
    response_model=PublicationResponse,
)
async def reconcile_not_published(
    publication_id: UUID,
    payload: PublicationReconcileNotPublishedRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> PublicationResponse:
    return await PublicationService(session).reconcile_not_published(
        publication_id, user, note=payload.note
    )


@router.post(
    "/{publication_id}/recover-stuck",
    response_model=PublicationResponse,
)
async def recover_stuck_publication(
    publication_id: UUID, user: CurrentUser, session: SessionDependency
) -> PublicationResponse:
    publication = await PublicationService(session).recover_one_stuck(publication_id)
    return await PublicationService(session)._response(publication)
