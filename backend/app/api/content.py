import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.errors import AppError as ContentAppError
from app.models.agent import AgentStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
    ContentVersionSource,
)
from app.models.knowledge_pack import KnowledgePackItem
from app.models.publication_plan import PublicationPlanItem
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.agents import AgentRepository
from app.schemas.approval import RequiredApprovalComment
from app.schemas.content import (
    ContentApprovalHistory,
    ContentApprovalRequest,
    ContentDerivationResponse,
    ContentListItem,
    ContentRejectionRequest,
    ContentResponse,
    ContentSourceResponse,
    ContentVersionResponse,
    ContentVersionSummary,
)
from app.schemas.task import TaskCreate
from app.services.activity_log_service import ActivityLogService
from app.services.content_service import ContentService
from app.services.social_content_quality import (
    social_text_quality_errors,
    social_text_quality_message,
)
from app.services.task_dispatcher_service import TaskDispatcherService
from app.services.task_service import TaskService

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


async def _plan_context(session: AsyncSession, item: ContentItem) -> dict[str, Any]:
    raw = (item.metadata_ or {}).get("publication_plan_item_id")
    try:
        item_id = UUID(str(raw)) if raw else None
    except ValueError:
        item_id = None
    plan_item = await session.get(PublicationPlanItem, item_id) if item_id else None
    return {
        "current_version_id": item.current_version_id,
        "publication_plan_item_id": item_id,
        "plan_channel": plan_item.channel.value if plan_item else None,
        "plan_scheduled_at": plan_item.scheduled_at if plan_item else None,
    }


def _response(
    item: Any,
    approvals: list[Approval] | None = None,
    plan_context: dict[str, Any] | None = None,
    derivations: list[ContentDerivationResponse] | None = None,
) -> ContentResponse:
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
            derivations=derivations or [],
        )
    )
    approved_version_id = None
    for approval in reversed(approvals or []):
        if approval.status is ApprovalStatus.APPROVED:
            raw_version_id = approval.subject_snapshot.get("content_version_id")
            if item.parent_content_item_id is not None:
                for post in approval.subject_snapshot.get("posts", []):
                    if str(post.get("content_item_id")) == str(item.id):
                        raw_version_id = post.get("content_version_id")
                        break
            if raw_version_id:
                approved_version_id = UUID(str(raw_version_id))
                break
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
        approved_version_id=approved_version_id,
        **(plan_context or {}),
        source_task_id=item.source_task_id,
        author_agent_id=item.author_agent_id,
        current_version=current_response,
        versions=[_summary(version) for version in versions],
        approval_history=[
            ContentApprovalHistory(
                id=approval.id,
                subject_version=approval.subject_version,
                status=approval.status.value,
                comment=approval.comment,
                reviewed_by_user_id=approval.reviewed_by_user_id,
                created_at=approval.created_at,
                resolved_at=approval.resolved_at,
            )
            for approval in sorted(approvals or [], key=lambda value: value.created_at)
        ],
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
    content_ids = {item.id for item in items}
    parent_ids = {item.parent_content_item_id for item in items if item.parent_content_item_id}
    approval_rows = list(
        (
            await session.scalars(
                select(Approval)
                .where(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id.in_(content_ids | parent_ids),
                    Approval.status == ApprovalStatus.APPROVED,
                )
                .order_by(Approval.created_at.desc())
            )
        ).all()
    ) if content_ids or parent_ids else []
    approved_versions: dict[UUID, UUID] = {}
    for approval in approval_rows:
        raw_version_id = approval.subject_snapshot.get("content_version_id")
        if approval.object_id in content_ids and raw_version_id:
            try:
                approved_versions.setdefault(approval.object_id, UUID(str(raw_version_id)))
            except ValueError:
                pass
        for post in approval.subject_snapshot.get("posts", []):
            try:
                post_id = UUID(str(post.get("content_item_id")))
                version_id = UUID(str(post.get("content_version_id")))
            except (ValueError, TypeError):
                continue
            if post_id in content_ids:
                approved_versions.setdefault(post_id, version_id)
    approved_version_numbers: dict[UUID, int] = {}
    if approved_versions:
        approved_version_numbers = dict(
            (
                await session.execute(
                    select(ContentVersion.id, ContentVersion.version_number).where(
                        ContentVersion.id.in_(set(approved_versions.values()))
                    )
                )
            ).all()
        )

    def approved_version_number(item_id: UUID) -> int | None:
        version_id = approved_versions.get(item_id)
        return approved_version_numbers.get(version_id) if version_id is not None else None

    current_version_ids = {item.current_version_id for item in items if item.current_version_id}
    source_by_derived_version: dict[UUID, tuple[UUID, str]] = {}
    if current_version_ids:
        source_rows = (
            await session.execute(
                select(
                    ContentDerivation.derived_content_version_id,
                    ContentVersion.content_item_id,
                    ContentItem.title,
                )
                .join(
                    ContentVersion,
                    ContentVersion.id == ContentDerivation.source_content_version_id,
                )
                .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
                .where(ContentDerivation.derived_content_version_id.in_(current_version_ids))
                .order_by(ContentDerivation.source_section_key)
            )
        ).all()
        for derived_version_id, source_item_id, source_title in source_rows:
            source_by_derived_version.setdefault(
                derived_version_id, (source_item_id, source_title)
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
            approved_version_id=approved_versions.get(item.id),
            approved_version_number=approved_version_number(item.id),
            source_content_item_id=(
                source_by_derived_version.get(item.current_version_id, (None, None))[0]
                if item.current_version_id
                else None
            ),
            source_content_item_title=(
                source_by_derived_version.get(item.current_version_id, (None, None))[1]
                if item.current_version_id
                else None
            ),
            **await _plan_context(session, item),
        )
        for item in items
    ]


@router.get("/{content_id}", response_model=ContentResponse)
async def get_content(
    content_id: UUID,
    _user: CurrentUser,
    session: SessionDependency,
) -> ContentResponse:
    item = await ContentService(session).get(content_id)
    approvals = list(
        (
            await session.scalars(
                select(Approval)
                .where(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id == content_id,
                )
                .order_by(Approval.created_at)
            )
        ).all()
    )
    if not approvals and item.parent_content_item_id is not None:
        parent_approval = await session.scalar(
            select(Approval)
            .where(
                Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                Approval.object_id == item.parent_content_item_id,
                Approval.status == ApprovalStatus.APPROVED,
            )
            .order_by(Approval.resolved_at.desc().nullslast())
        )
        if parent_approval is not None:
            approvals.append(parent_approval)
    derivation_rows = list(
        (
            await session.execute(
                select(
                    ContentDerivation,
                    ContentVersion.version_number,
                    ContentVersion.content_item_id,
                    ContentItem.title,
                )
                .join(
                    ContentVersion,
                    ContentVersion.id == ContentDerivation.source_content_version_id,
                )
                .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
                .where(ContentDerivation.derived_content_version_id == item.current_version_id)
                .order_by(ContentDerivation.source_section_key)
            )
        )
    ).all() if item.current_version_id else []
    derivations = [
        ContentDerivationResponse(
            source_content_item_id=content_item_id,
            source_content_item_title=title,
            source_content_version_id=derivation.source_content_version_id,
            source_version_number=version_number,
            section_key=derivation.source_section_key,
        )
        for derivation, version_number, content_item_id, title in derivation_rows
    ]
    return _response(item, approvals, await _plan_context(session, item), derivations)


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
    if item.status is ContentStatus.ARCHIVED:
        raise ContentAppError(
            "CONTENT_ARCHIVED",
            "Архивный материал доступен только для просмотра истории.",
            409,
        )
    pending_approvals = list(
        (
            await session.scalars(
                select(Approval)
                .where(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id == content_id,
                    Approval.status == ApprovalStatus.PENDING,
                )
                .with_for_update()
            )
        ).all()
    )
    current_version_id = str(item.current_version_id)
    approval = next(
        (
            candidate
            for candidate in pending_approvals
            if str(candidate.subject_snapshot.get("content_version_id")) == current_version_id
        ),
        pending_approvals[0] if pending_approvals else None,
    )
    for stale in pending_approvals:
        if approval is not None and stale.id != approval.id:
            stale.status = ApprovalStatus.REVISION_REQUESTED
            stale.comment = "Согласование автоматически заменено новой версией контента."
            stale.resolved_at = datetime.now(UTC)
            stale.metadata_ = {
                **stale.metadata_,
                "superseded_by_content_version_id": current_version_id,
            }
    if approval is None:
        requested = (
            await session.execute(
                select(Approval)
                .where(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id == content_id,
                    Approval.status == ApprovalStatus.REVISION_REQUESTED,
                )
                .order_by(Approval.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if requested is not None:
            existing = await session.scalar(
                select(Task).where(
                    Task.task_type == TaskType.CONTENT_REVISION,
                    Task.input_data["approval_id"].as_string() == str(requested.id),
                    Task.status.in_(
                        [TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED]
                    ),
                )
            )
            if existing is not None:
                return _response(await ContentService(session).get(content_id))
        from app.core.errors import AppError

        if item.status is (
            ContentStatus.APPROVED if status is ApprovalStatus.APPROVED else ContentStatus.REJECTED
        ):
            return _response(await ContentService(session).get(content_id))
        raise AppError("CONTENT_APPROVAL_NOT_FOUND", "Согласование контента не найдено.", 404)
    version_id = approval.subject_snapshot.get("content_version_id")
    current_version = await session.scalar(
        select(ContentVersion).where(ContentVersion.id == item.current_version_id)
    )
    snapshot_hash = approval.subject_snapshot.get("content_hash")
    snapshot_number = approval.subject_snapshot.get("version_number")
    if (
        current_version is None
        or str(item.current_version_id) != str(version_id)
        or (snapshot_number is not None and current_version.version_number != int(snapshot_number))
        or (
            snapshot_hash is not None
            and hashlib.sha256(current_version.content.encode()).hexdigest() != str(snapshot_hash)
        )
    ):
        from app.core.errors import AppError

        raise AppError(
            "STALE_APPROVAL_VERSION",
            "Согласование относится к устаревшей версии контента.",
            409,
        )
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
            child_version = (
                await session.scalar(
                    select(ContentVersion).where(ContentVersion.id == child.current_version_id)
                )
                if child is not None
                else None
            )
            child_hash = snapshot_post.get("content_hash")
            child_number = snapshot_post.get("version_number")
            if (
                child is None
                or child_version is None
                or str(child.current_version_id) != str(snapshot_post.get("content_version_id"))
                or (child_number is not None and child_version.version_number != int(child_number))
                or (
                    child_hash is not None
                    and hashlib.sha256(child_version.content.encode()).hexdigest()
                    != str(child_hash)
                )
            ):
                from app.core.errors import AppError

                raise AppError(
                    "STALE_APPROVAL_VERSION",
                    "Согласование содержит устаревшую версию поста.",
                    409,
                )
            if status is ApprovalStatus.APPROVED:
                child_version = await session.scalar(
                    select(ContentVersion).where(ContentVersion.id == child.current_version_id)
                )
                quality_errors = social_text_quality_errors(
                    child_version.content if child_version else ""
                )
                if quality_errors:
                    raise ContentAppError(
                        "SOCIAL_POST_QUALITY_INVALID",
                        social_text_quality_message(quality_errors),
                        422,
                    )
    if item.content_type is ContentType.SOCIAL_POST:
        version = await session.scalar(
            select(ContentVersion).where(ContentVersion.id == item.current_version_id)
        )
        quality_errors = social_text_quality_errors(version.content if version else "")
        if quality_errors:
            raise ContentAppError(
                "SOCIAL_POST_QUALITY_INVALID",
                social_text_quality_message(quality_errors),
                422,
            )
    approval.status = status
    approval.reviewed_by_user_id = user.id
    approval.comment = comment
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
                    .where(ContentItem.status != ContentStatus.ARCHIVED)
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
                    .where(ContentItem.status != ContentStatus.ARCHIVED)
                    .with_for_update()
                )
            ).all()
        )
        for child in children:
            child.status = ContentStatus.REJECTED
    if item.content_type is ContentType.ARTICLE and status is ApprovalStatus.APPROVED:
        from app.services.task_service import TaskService

        await TaskService(session).refresh_dependents_for_content(item.id)
    await session.commit()
    await ActivityLogService(session).record(
        "CONTENT_APPROVED" if status is ApprovalStatus.APPROVED else "CONTENT_REJECTED",
        operation_key=f"content-resolution:{approval.id}:{status.value}",
        campaign_id=item.campaign_id,
        user_id=user.id,
        content_item_id=item.id,
        approval_id=approval.id,
    )
    await session.commit()
    return await get_content(content_id, user, session)


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


@router.post("/{content_id}/request-revision", response_model=ContentResponse, status_code=202)
async def request_revision(
    content_id: UUID,
    payload: RequiredApprovalComment,
    user: CurrentUser,
    session: SessionDependency,
) -> ContentResponse:
    item = (
        await session.execute(
            select(ContentItem).where(ContentItem.id == content_id).with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        from app.core.errors import AppError

        raise AppError("CONTENT_NOT_FOUND", "Материал не найден.", 404)
    if item.content_type not in {ContentType.ARTICLE, ContentType.SOCIAL_POST_PACK}:
        from app.core.errors import AppError

        raise AppError(
            "CONTENT_REVISION_NOT_SUPPORTED", "Этот тип контента нельзя дорабатывать.", 409
        )
    pending_approvals = list(
        (
            await session.scalars(
                select(Approval)
                .where(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id == content_id,
                    Approval.status == ApprovalStatus.PENDING,
                )
                .with_for_update()
            )
        ).all()
    )
    current_version_id = str(item.current_version_id)
    approval = next(
        (
            candidate
            for candidate in pending_approvals
            if str(candidate.subject_snapshot.get("content_version_id")) == current_version_id
        ),
        pending_approvals[0] if pending_approvals else None,
    )
    for stale in pending_approvals:
        if approval is not None and stale.id != approval.id:
            stale.status = ApprovalStatus.REVISION_REQUESTED
            stale.comment = "Согласование автоматически заменено новой версией контента."
            stale.resolved_at = datetime.now(UTC)
            stale.metadata_ = {
                **stale.metadata_,
                "superseded_by_content_version_id": current_version_id,
            }
    if approval is None:
        from app.core.errors import AppError

        raise AppError("CONTENT_APPROVAL_NOT_FOUND", "Ожидающее согласование не найдено.", 404)
    if str(item.current_version_id) != str(approval.subject_snapshot.get("content_version_id")):
        from app.core.errors import AppError

        raise AppError(
            "STALE_APPROVAL_VERSION",
            "Согласование относится к устаревшей версии контента.",
            409,
        )
    if item.content_type is ContentType.SOCIAL_POST_PACK:
        post_snapshots = approval.subject_snapshot.get("posts", [])
        if post_snapshots:
            child_ids = [UUID(str(post["content_item_id"])) for post in post_snapshots]
            children = {
                child.id: child
                for child in (
                    await session.scalars(
                        select(ContentItem).where(ContentItem.id.in_(child_ids)).with_for_update()
                    )
                ).all()
            }
            if any(
                child_id not in children
                or str(children[child_id].current_version_id) != str(post.get("content_version_id"))
                for child_id, post in zip(child_ids, post_snapshots, strict=False)
            ):
                raise AppError(
                    "STALE_APPROVAL_VERSION",
                    "Согласование содержит устаревшую версию поста.",
                    409,
                )
    existing = await session.scalar(
        select(Task).where(
            Task.task_type == TaskType.CONTENT_REVISION,
            Task.input_data["approval_id"].as_string() == str(approval.id),
            Task.status.in_([TaskStatus.READY, TaskStatus.IN_PROGRESS]),
        )
    )
    if existing:
        return _response(await ContentService(session).get(content_id))
    slug = "writer" if item.content_type is ContentType.ARTICLE else "smm_manager"
    agent = await AgentRepository(session).get_by_slug(slug)
    from app.core.errors import AppError

    if agent is None or agent.status is not AgentStatus.ACTIVE:
        raise AppError("REQUIRED_AGENT_INACTIVE", "Исполнитель доработки недоступен.", 409)
    immutable_context: dict[str, object] = {}
    if item.content_type is ContentType.ARTICLE:
        pack_ids = list(
            (
                await session.scalars(
                    select(KnowledgePackItem.knowledge_pack_id)
                    .join(
                        ContentVersionSource,
                        ContentVersionSource.knowledge_pack_item_id == KnowledgePackItem.id,
                    )
                    .where(ContentVersionSource.content_version_id == item.current_version_id)
                )
            ).all()
        )
        immutable_context["knowledge_pack_ids"] = [str(value) for value in dict.fromkeys(pack_ids)]
    else:
        article_version_ids = list(
            (
                await session.scalars(
                    select(ContentDerivation.source_content_version_id).where(
                        ContentDerivation.derived_content_version_id.in_(
                            select(ContentItem.current_version_id).where(
                                ContentItem.parent_content_item_id == item.id
                            )
                        )
                    )
                )
            ).all()
        )
        immutable_context["article_version_ids"] = [
            str(value) for value in dict.fromkeys(article_version_ids)
        ]
    approval.status = ApprovalStatus.REVISION_REQUESTED
    approval.reviewed_by_user_id = user.id
    approval.comment = payload.comment
    approval.resolved_at = datetime.now(UTC)
    task = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=item.campaign_id,
            task_type=TaskType.CONTENT_REVISION,
            title=f"Доработать: {item.title}",
            description="Подготовить новую версию контента по комментарию пользователя.",
            assigned_agent_id=agent.id,
            input_data={
                "content_item_id": str(item.id),
                "base_content_version_id": str(item.current_version_id),
                "approval_id": str(approval.id),
                "revision_comment": payload.comment,
                "requested_by_user_id": str(user.id),
                "original_task_id": str(item.source_task_id),
                "revision_target_type": item.content_type.value,
                "immutable_source_context": immutable_context,
            },
        ),
        commit=False,
    )
    await session.commit()
    await ActivityLogService(session).record(
        "CONTENT_REVISION_REQUESTED",
        operation_key=f"revision-request:{approval.id}",
        campaign_id=item.campaign_id,
        task_id=task.id,
        user_id=user.id,
        content_item_id=item.id,
        approval_id=approval.id,
        metadata={"revision_comment": payload.comment},
    )
    await session.commit()
    await TaskDispatcherService(session).dispatch_ready_tasks()
    return _response(await ContentService(session).get(content_id))
