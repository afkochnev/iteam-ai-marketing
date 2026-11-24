from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query

from app.api.dependencies import CurrentSnapshotUser, ReadSnapshotSession
from app.schemas.hypothesis import CampaignHypothesesResponse, HypothesisVersionResponse
from app.services.campaign_hypothesis_service import CampaignHypothesisService

router = APIRouter(prefix="/campaigns/{campaign_id}/hypotheses", tags=["campaign-hypotheses"])


@router.get("", response_model=CampaignHypothesesResponse)
async def list_campaign_hypotheses(
    campaign_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CampaignHypothesesResponse:
    return await CampaignHypothesisService(session).list_for_campaign(
        user.id, campaign_id, limit=limit, offset=offset
    )


@router.get("/{hypothesis_id}/versions/{version_number}", response_model=HypothesisVersionResponse)
async def get_campaign_hypothesis_version(
    campaign_id: UUID,
    hypothesis_id: UUID,
    version_number: Annotated[int, Path(ge=1, le=2_147_483_647)],
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
) -> HypothesisVersionResponse:
    return await CampaignHypothesisService(session).get_version(
        user.id, campaign_id, hypothesis_id, version_number
    )
