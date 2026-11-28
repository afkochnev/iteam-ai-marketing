from uuid import UUID

from fastapi import APIRouter, Response

from app.api.dependencies import (
    CurrentSnapshotUser,
    CurrentUser,
    ReadSnapshotSession,
    SessionDependency,
)
from app.models.studio_wordpress import WordPressAction
from app.services.studio_wordpress import (
    StudioWordPressService,
    WordPressConfirmation,
    WordPressDeliveryView,
    WordPressPublicationConfirmation,
)

router = APIRouter(
    prefix="/campaigns/{campaign_id}/studio/versions/{version_id}/wordpress",
    tags=["studio-wordpress"],
)


@router.get("", response_model=WordPressDeliveryView)
async def wordpress_status(
    campaign_id: UUID,
    version_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    response: Response,
) -> WordPressDeliveryView:
    response.headers["Cache-Control"] = "private, no-store"
    return await StudioWordPressService(session).status(user.id, campaign_id, version_id)


@router.post("/draft", response_model=WordPressDeliveryView)
async def request_wordpress_draft(
    campaign_id: UUID,
    version_id: UUID,
    confirmation: WordPressConfirmation,
    user: CurrentUser,
    session: SessionDependency,
) -> WordPressDeliveryView:
    result = await StudioWordPressService(session).request_draft(
        user.id, campaign_id, version_id, confirmation
    )
    await session.commit()
    return result


@router.post("/publish", response_model=WordPressDeliveryView)
async def request_wordpress_publication(
    campaign_id: UUID,
    version_id: UUID,
    confirmation: WordPressPublicationConfirmation,
    user: CurrentUser,
    session: SessionDependency,
) -> WordPressDeliveryView:
    result = await StudioWordPressService(session).request_publication(
        user.id, campaign_id, version_id, confirmation
    )
    await session.commit()
    return result


@router.post("/{action}/reconcile", response_model=WordPressDeliveryView)
async def request_wordpress_reconciliation(
    campaign_id: UUID,
    version_id: UUID,
    action: WordPressAction,
    confirmation: WordPressConfirmation,
    user: CurrentUser,
    session: SessionDependency,
) -> WordPressDeliveryView:
    result = await StudioWordPressService(session).request_reconciliation(
        user.id, campaign_id, version_id, action, confirmation
    )
    await session.commit()
    return result
