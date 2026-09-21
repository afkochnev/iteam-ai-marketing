from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, UploadFile, status

from app.api.dependencies import AdminUser, CurrentUser, SessionDependency
from app.models.knowledge import KnowledgeItemStatus
from app.schemas.knowledge import (
    KnowledgeItemResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceResponse,
    KnowledgeStoreResponse,
)
from app.services.knowledge_search_service import KnowledgeSearchService
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_store_service import KnowledgeStoreService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/store", response_model=KnowledgeStoreResponse | None)
async def get_store(
    _user: CurrentUser, session: SessionDependency
) -> KnowledgeStoreResponse | None:
    store = await KnowledgeStoreService(session).get()
    return KnowledgeStoreResponse.model_validate(store) if store else None


@router.post("/store/initialize", response_model=KnowledgeStoreResponse)
async def initialize_store(_admin: AdminUser, session: SessionDependency) -> KnowledgeStoreResponse:
    return KnowledgeStoreResponse.model_validate(await KnowledgeStoreService(session).initialize())


@router.get("/sources", response_model=list[KnowledgeSourceResponse])
async def list_sources(
    _user: CurrentUser, session: SessionDependency
) -> list[KnowledgeSourceResponse]:
    return [
        KnowledgeSourceResponse.model_validate(item)
        for item in await KnowledgeService(session).repository.list_sources()
    ]


@router.get("/items", response_model=list[KnowledgeItemResponse])
async def list_items(
    _user: CurrentUser,
    session: SessionDependency,
    item_status: Annotated[KnowledgeItemStatus | None, Query(alias="status")] = None,
    source_id: UUID | None = None,
    content_type: str | None = None,
) -> list[KnowledgeItemResponse]:
    items = await KnowledgeService(session).list_items(
        status=item_status, source_id=source_id, content_type=content_type
    )
    return [KnowledgeItemResponse.model_validate(item) for item in items]


@router.post("/upload", response_model=KnowledgeItemResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_item(
    admin: AdminUser,
    session: SessionDependency,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    author: Annotated[str | None, Form()] = None,
) -> KnowledgeItemResponse:
    service = KnowledgeService(session)
    item = await service.upload(
        filename=file.filename or "",
        content=await file.read(),
        mime_type=file.content_type,
        title=title,
        author=author,
        created_by=admin.id,
    )
    item = await service.enqueue_indexing(item)
    return KnowledgeItemResponse.model_validate(item)


@router.get("/items/{item_id}", response_model=KnowledgeItemResponse)
async def get_item(
    item_id: UUID, _user: CurrentUser, session: SessionDependency
) -> KnowledgeItemResponse:
    return KnowledgeItemResponse.model_validate(await KnowledgeService(session).get_item(item_id))


@router.post("/items/{item_id}/retry", response_model=KnowledgeItemResponse, status_code=202)
async def retry_item(
    item_id: UUID, _admin: AdminUser, session: SessionDependency
) -> KnowledgeItemResponse:
    return KnowledgeItemResponse.model_validate(await KnowledgeService(session).retry(item_id))


@router.post("/items/{item_id}/archive", response_model=KnowledgeItemResponse)
async def archive_item(
    item_id: UUID, _admin: AdminUser, session: SessionDependency
) -> KnowledgeItemResponse:
    return KnowledgeItemResponse.model_validate(await KnowledgeService(session).archive(item_id))


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    payload: KnowledgeSearchRequest, _user: CurrentUser, session: SessionDependency
) -> KnowledgeSearchResponse:
    return await KnowledgeSearchService(session).search(payload.query, payload.max_results)
