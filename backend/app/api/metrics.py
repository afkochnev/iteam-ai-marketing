from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.content import ContentChannel
from app.schemas.publication import CampaignPerformanceResponse
from app.services.metrics_service import MetricsService

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
