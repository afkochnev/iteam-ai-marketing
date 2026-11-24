from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, ReadSnapshotSession
from app.schemas.distribution_workspace import DistributionWorkspaceResponse
from app.services.distribution_workspace_service import DistributionWorkspaceService

router = APIRouter(tags=["distribution"])


@router.get("/distribution-workspace", response_model=DistributionWorkspaceResponse)
async def get_distribution_workspace(
    user: CurrentUser,
    session: ReadSnapshotSession,
    campaign_id: UUID | None = None,
    publication_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> DistributionWorkspaceResponse:
    return await DistributionWorkspaceService(session).get(
        user,
        campaign_id=campaign_id,
        publication_id=publication_id,
        limit=limit,
        cursor=cursor,
    )
