from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser
from app.core.database import async_session_factory
from app.schemas.distribution_workspace import DistributionWorkspaceResponse
from app.services.distribution_workspace_service import DistributionWorkspaceService

router = APIRouter(tags=["distribution"])


async def get_distribution_read_session() -> AsyncIterator[AsyncSession]:
    """A dedicated, injectable snapshot, separate from CurrentUser's session.

    Connection characteristics are set before the first query. Closing always
    rolls back this read-only transaction; no commit or application writes run.
    """
    async with async_session_factory(autoflush=False) as session:
        await session.connection(
            execution_options={
                "isolation_level": "REPEATABLE READ",
                "postgresql_readonly": True,
            }
        )
        yield session


DistributionReadSession = Annotated[AsyncSession, Depends(get_distribution_read_session)]


@router.get("/distribution-workspace", response_model=DistributionWorkspaceResponse)
async def get_distribution_workspace(
    user: CurrentUser,
    session: DistributionReadSession,
    campaign_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> DistributionWorkspaceResponse:
    return await DistributionWorkspaceService(session).get(
        user,
        campaign_id=campaign_id,
        limit=limit,
        cursor=cursor,
    )
