from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.knowledge_pack import KnowledgePackStatus
from app.schemas.knowledge_pack import KnowledgePackResponse
from app.services.knowledge_pack_service import KnowledgePackService

router = APIRouter(prefix="/knowledge-packs", tags=["knowledge-packs"])


@router.get("", response_model=list[KnowledgePackResponse])
async def list_knowledge_packs(
    _user: CurrentUser,
    session: SessionDependency,
    campaign_id: UUID | None = None,
    task_id: UUID | None = None,
    pack_status: Annotated[KnowledgePackStatus | None, Query(alias="status")] = None,
) -> list[KnowledgePackResponse]:
    packs = await KnowledgePackService(session).list(
        campaign_id=campaign_id, task_id=task_id, status=pack_status
    )
    return [KnowledgePackResponse.model_validate(pack) for pack in packs]


@router.get("/{pack_id}", response_model=KnowledgePackResponse)
async def get_knowledge_pack(
    pack_id: UUID, _user: CurrentUser, session: SessionDependency
) -> KnowledgePackResponse:
    return KnowledgePackResponse.model_validate(await KnowledgePackService(session).get(pack_id))
