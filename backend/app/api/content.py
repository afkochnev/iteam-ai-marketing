from uuid import UUID
from typing import Any
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.dependencies import get_current_user
from app.core.database import get_db_session
from app.models.content import ContentStatus, ContentType
from app.models.user import User
from app.schemas.content import ContentListItem, ContentResponse, ContentSourceResponse, ContentVersionResponse, ContentVersionSummary
from app.services.content_service import ContentService

router = APIRouter(prefix="/content", tags=["content"])
def _source(source: Any) -> ContentSourceResponse:
    item = source.knowledge_pack_item
    return ContentSourceResponse(knowledge_pack_item_id=source.knowledge_pack_item_id, source_title=item.source_title, filename=item.filename, excerpt=item.excerpt, relevance_score=item.relevance_score, section_key=source.section_key)
def _summary(version: Any) -> ContentVersionSummary: return ContentVersionSummary(id=version.id, version_number=version.version_number, change_description=version.change_description, created_at=version.created_at)
def _response(item: Any) -> ContentResponse:
    versions = sorted(item.versions, key=lambda value: value.version_number)
    current = next((version for version in versions if version.id == item.current_version_id), None)
    current_response = None if current is None else ContentVersionResponse(**_summary(current).model_dump(), content=current.content, structured_content=current.structured_content, sources=[_source(source) for source in current.sources])
    return ContentResponse(id=item.id, campaign_id=item.campaign_id, content_type=item.content_type, title=item.title, status=item.status, current_version_number=current.version_number if current else None, created_at=item.created_at, updated_at=item.updated_at, source_task_id=item.source_task_id, author_agent_id=item.author_agent_id, current_version=current_response, versions=[_summary(version) for version in versions])
@router.get("", response_model=list[ContentListItem])
async def list_content(campaign_id: UUID | None = None, content_type: ContentType | None = None, status: ContentStatus | None = None, author_agent_id: UUID | None = None, _user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)) -> list[ContentListItem]:
    items = await ContentService(session).list(campaign_id=campaign_id, content_type=content_type, status=status, author_agent_id=author_agent_id)
    return [ContentListItem(id=item.id, campaign_id=item.campaign_id, content_type=item.content_type, title=item.title, status=item.status, current_version_number=next((v.version_number for v in item.versions if v.id == item.current_version_id), None), created_at=item.created_at, updated_at=item.updated_at) for item in items]
@router.get("/{content_id}", response_model=ContentResponse)
async def get_content(content_id: UUID, _user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)) -> ContentResponse:
    return _response(await ContentService(session).get(content_id))
@router.get("/{content_id}/versions", response_model=list[ContentVersionSummary])
async def list_versions(content_id: UUID, _user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)) -> list[ContentVersionSummary]:
    item = await ContentService(session).get(content_id)
    return [_summary(version) for version in sorted(item.versions, key=lambda value: value.version_number)]
