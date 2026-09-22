from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import ContentItem, ContentStatus, ContentType
from app.models.user import User
from app.schemas.content import (
    ContentApprovalRequest,
    ContentListItem,
    ContentRejectionRequest,
    ContentResponse,
    ContentSourceResponse,
    ContentVersionResponse,
    ContentVersionSummary,
)
from app.services.content_service import ContentService

router = APIRouter(prefix="/content", tags=["content"])


def _source(source: Any) -> ContentSourceResponse:
    item = source.knowledge_pack_item
    return ContentSourceResponse(
        knowledge_pack_item_id=source.knowledge_pack_item_id,
        source_title=item.source_title,
        filename=item.filename,
        excerpt=item.excerpt,
        relevance_score=item.relevance_score,
        section_key=source.section_key,
    )


def _summary(version: Any) -> ContentVersionSummary:
    return ContentVersionSummary(
        id=version.id,
        version_number=version.version_number,
        change_description=version.change_description,
        created_at=version.created_at,
    )


def _response(item: Any) -> ContentResponse:
    versions = sorted(item.versions, key=lambda value: value.version_number)
    current = next((version for version in versions if version.id == item.current_version_id), None)
    current_response = (
        None
        if current is None
        else ContentVersionResponse(
            **_summary(current).model_dump(),
            content=current.content,
            structured_content=current.structured_content,
            sources=[_source(source) for source in current.sources],
        )
    )
    return ContentResponse(
        id=item.id,
        campaign_id=item.campaign_id,
        content_type=item.content_type,
        title=item.title,
        status=item.status,
        current_version_number=current.version_number if current else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
        parent_content_item_id=item.parent_content_item_id,
        channel=item.channel,
        source_task_id=item.source_task_id,
        author_agent_id=item.author_agent_id,
        current_version=current_response,
        versions=[_summary(version) for version in versions],
    )


@router.get("", response_model=list[ContentListItem])
async def list_content(
    _user: CurrentUser,
    session: SessionDependency,
    campaign_id: UUID | None = None,
    content_type: ContentType | None = None,
    status: ContentStatus | None = None,
    author_agent_id: UUID | None = None,
) -> list[ContentListItem]:
    items = await ContentService(session).list(
        campaign_id=campaign_id,
        content_type=content_type,
        status=status,
        author_agent_id=author_agent_id,
    )
    return [
        ContentListItem(
            id=item.id,
            campaign_id=item.campaign_id,
            content_type=item.content_type,
            title=item.title,
            status=item.status,
            current_version_number=next(
                (v.version_number for v in item.versions if v.id == item.current_version_id), None
            ),
            created_at=item.created_at,
            updated_at=item.updated_at,
            parent_content_item_id=item.parent_content_item_id,
            channel=item.channel,
        )
        for item in items
    ]


@router.get("/{content_id}", response_model=ContentResponse)
async def get_content(
    content_id: UUID,
    _user: CurrentUser,
    session: SessionDependency,
) -> ContentResponse:
    return _response(await ContentService(session).get(content_id))


@router.get("/{content_id}/versions", response_model=list[ContentVersionSummary])
async def list_versions(
    content_id: UUID,
    _user: CurrentUser,
    session: SessionDependency,
) -> list[ContentVersionSummary]:
    item = await ContentService(session).get(content_id)
    return [
        _summary(version)
        for version in sorted(item.versions, key=lambda value: value.version_number)
    ]


async def _resolve_content(
    content_id: UUID, user: User, session: AsyncSession, status: ApprovalStatus, comment: str | None
) -> ContentResponse:
    item = (
        await session.execute(
            select(ContentItem).where(ContentItem.id == content_id).with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        from app.core.errors import AppError

        raise AppError("CONTENT_NOT_FOUND", "Материал не найден.", 404)
    approval = (
        await session.execute(
            select(Approval)
            .where(
                Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                Approval.object_id == content_id,
                Approval.status == ApprovalStatus.PENDING,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if approval is None:
        from app.core.errors import AppError

        if item.status is (
            ContentStatus.APPROVED if status is ApprovalStatus.APPROVED else ContentStatus.REJECTED
        ):
            return _response(await ContentService(session).get(content_id))
        raise AppError("CONTENT_APPROVAL_NOT_FOUND", "Согласование контента не найдено.", 404)
    version_id = approval.subject_snapshot.get("content_version_id")
    if str(item.current_version_id) != str(version_id):
        from app.core.errors import AppError

        raise AppError("CONTENT_APPROVAL_STALE", "Согласование относится к устаревшей версии.", 409)
    if item.content_type is ContentType.SOCIAL_POST_PACK:
        snapshot_posts = approval.subject_snapshot.get("posts", [])
        child_ids = [
            UUID(str(post["content_item_id"]))
            for post in snapshot_posts
            if post.get("content_item_id")
        ]
        children = list(
            (
                await session.scalars(
                    select(ContentItem).where(ContentItem.id.in_(child_ids)).with_for_update()
                )
            ).all()
        )
        children_by_id = {child.id: child for child in children}
        for snapshot_post in snapshot_posts:
            child = children_by_id.get(UUID(str(snapshot_post["content_item_id"])))
            if child is None or str(child.current_version_id) != str(
                snapshot_post.get("content_version_id")
            ):
                from app.core.errors import AppError

                raise AppError(
                    "CONTENT_APPROVAL_STALE", "Согласование содержит устаревшую версию поста.", 409
                )
    approval.status = status
    approval.reviewed_by_user_id = user.id
    approval.comment = comment
    from datetime import UTC, datetime

    approval.resolved_at = datetime.now(UTC)
    item.status = (
        ContentStatus.APPROVED if status is ApprovalStatus.APPROVED else ContentStatus.REJECTED
    )
    if item.content_type is ContentType.SOCIAL_POST_PACK and status is ApprovalStatus.APPROVED:
        children = list(
            (
                await session.scalars(
                    select(ContentItem)
                    .where(ContentItem.parent_content_item_id == item.id)
                    .with_for_update()
                )
            ).all()
        )
        for child in children:
            child.status = ContentStatus.APPROVED
    if item.content_type is ContentType.SOCIAL_POST_PACK and status is ApprovalStatus.REJECTED:
        children = list(
            (
                await session.scalars(
                    select(ContentItem)
                    .where(ContentItem.parent_content_item_id == item.id)
                    .with_for_update()
                )
            ).all()
        )
        for child in children:
            child.status = ContentStatus.REJECTED
    await session.commit()
    return _response(await ContentService(session).get(content_id))


@router.post("/{content_id}/approve", response_model=ContentResponse)
async def approve_content(
    content_id: UUID,
    payload: ContentApprovalRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> ContentResponse:
    return await _resolve_content(
        content_id, user, session, ApprovalStatus.APPROVED, payload.comment
    )


@router.post("/{content_id}/reject", response_model=ContentResponse)
async def reject_content(
    content_id: UUID,
    payload: ContentRejectionRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> ContentResponse:
    if not payload.comment.strip():
        from app.core.errors import AppError

        raise AppError("CONTENT_REJECTION_COMMENT_REQUIRED", "Причина отклонения обязательна.", 422)
    return await _resolve_content(
        content_id, user, session, ApprovalStatus.REJECTED, payload.comment
    )
