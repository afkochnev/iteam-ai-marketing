from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.dependencies import (
    CurrentSnapshotUser,
    CurrentUser,
    ReadSnapshotSession,
    SessionDependency,
)
from app.models.content import ContentChannel
from app.schemas.publication import (
    CampaignPerformanceResponse,
    CampaignWordPressPerformanceResponse,
)
from app.services.metrics_service import MetricsService
from app.services.wordpress_page_placement import WordPressPagePlacementService

router = APIRouter(prefix="/campaigns", tags=["metrics"])


@router.get("/{campaign_id}/performance", response_model=CampaignPerformanceResponse)
async def campaign_performance(
    campaign_id: UUID,
    _user: CurrentUser,
    session: SessionDependency,
    from_at: Annotated[datetime, Query(alias="from")],
    to_at: Annotated[datetime, Query(alias="to")],
    channel: ContentChannel | None = None,
) -> CampaignPerformanceResponse:
    return CampaignPerformanceResponse.model_validate(
        await MetricsService(session).campaign_performance(campaign_id, from_at, to_at, channel)
    )


@router.get(
    "/{campaign_id}/performance/wordpress", response_model=CampaignWordPressPerformanceResponse
)
async def campaign_wordpress_performance(
    campaign_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    response: Response,
) -> CampaignWordPressPerformanceResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return await WordPressPagePlacementService(session).read(user.id, campaign_id)
