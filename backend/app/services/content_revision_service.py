from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.models.publication_plan import PublicationPlan, PublicationPlanItem, PublicationPlanStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.agents import AgentRepository
from app.schemas.task import TaskCreate
from app.services.activity_log_service import ActivityLogService
from app.services.task_service import TaskService


async def prepare_content_revision(
    session: AsyncSession,
    content_id: UUID,
    user: User,
    comment: str,
    *,
    optimization_action_id: UUID | None = None,
    provenance: dict[str, Any] | None = None,
) -> Task:
    """Shared revision contract; caller owns commit and dispatch."""
    item = (
        await session.execute(
            select(ContentItem).where(ContentItem.id == content_id).with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        from app.core.errors import AppError

        raise AppError("CONTENT_NOT_FOUND", "Материал не найден.", 404)
    if item.content_type not in {
        ContentType.ARTICLE,
        ContentType.SOCIAL_POST,
        ContentType.SOCIAL_POST_PACK,
    }:
        from app.core.errors import AppError

        raise AppError(
            "CONTENT_REVISION_NOT_SUPPORTED", "Этот тип контента нельзя дорабатывать.", 409
        )
    if item.status not in {ContentStatus.WAITING_APPROVAL, ContentStatus.APPROVED}:
        raise ContentAppError(
            "CONTENT_NOT_REVISIONABLE",
            "Доработать можно только материал, ожидающий согласования или уже утверждённый.",
            409,
        )
    current_version_id = str(item.current_version_id)
    approved_base = item.status is ContentStatus.APPROVED
    if approved_base and item.content_type not in {
        ContentType.ARTICLE,
        ContentType.SOCIAL_POST,
    }:
        raise ContentAppError(
            "CONTENT_REVISION_NOT_SUPPORTED",
            "Для утверждённых пакетов постов новая AI-доработка пока недоступна.",
            409,
        )
    pending_approvals = (
        []
        if approved_base
        else list(
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
    )
    approval = next(
        (
            candidate
            for candidate in pending_approvals
            if str(candidate.subject_snapshot.get("content_version_id")) == current_version_id
        ),
        pending_approvals[0] if pending_approvals else None,
    )
    approved_base_approval = None
    if approved_base:
        approved_base_approval = await session.scalar(
            select(Approval)
            .where(
                Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                Approval.object_id == content_id,
                Approval.status == ApprovalStatus.APPROVED,
                Approval.subject_snapshot["content_version_id"].as_string() == current_version_id,
            )
            .order_by(Approval.resolved_at.desc().nullslast())
            .with_for_update()
        )
        if approved_base_approval is None:
            raise ContentAppError(
                "CONTENT_APPROVAL_NOT_FOUND",
                "Текущая утверждённая версия не найдена в истории согласований.",
                409,
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
        existing_revision = await session.scalar(
            select(Task).where(
                Task.task_type == TaskType.CONTENT_REVISION,
                Task.input_data["content_item_id"].as_string() == str(item.id),
                Task.input_data["base_content_version_id"].as_string()
                == str(item.current_version_id),
                Task.status.in_([TaskStatus.READY, TaskStatus.IN_PROGRESS]),
            )
        )
        if existing_revision is not None:
            raise ContentAppError(
                "CONTENT_REVISION_ALREADY_REQUESTED",
                "Для этой версии уже создана задача доработки.",
                409,
            )
        if not approved_base:
            from app.core.errors import AppError

            raise AppError("CONTENT_APPROVAL_NOT_FOUND", "Ожидающее согласование не найдено.", 404)
    if approval is not None and str(item.current_version_id) != str(
        approval.subject_snapshot.get("content_version_id")
    ):
        from app.core.errors import AppError

        raise AppError(
            "STALE_APPROVAL_VERSION",
            "Согласование относится к устаревшей версии контента.",
            409,
        )
    if item.content_type is ContentType.SOCIAL_POST_PACK:
        assert approval is not None
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
    existing_query = select(Task).where(
        Task.task_type == TaskType.CONTENT_REVISION,
        Task.input_data["content_item_id"].as_string() == str(item.id),
        Task.input_data["base_content_version_id"].as_string() == current_version_id,
        Task.status.in_([TaskStatus.READY, TaskStatus.IN_PROGRESS]),
    )
    if approval is not None:
        existing_query = existing_query.where(
            Task.input_data["approval_id"].as_string() == str(approval.id)
        )
    existing = await session.scalar(existing_query)
    if existing:
        raise ContentAppError(
            "CONTENT_REVISION_ALREADY_REQUESTED",
            "Для этой версии уже создана задача доработки.",
            409,
        )
    slug = "writer" if item.content_type is ContentType.ARTICLE else "smm_manager"
    agent = await AgentRepository(session).get_by_slug(slug)
    from app.core.errors import AppError

    if agent is None or agent.status is not AgentStatus.ACTIVE:
        raise AppError("REQUIRED_AGENT_INACTIVE", "Исполнитель доработки недоступен.", 409)
    immutable_context: dict[str, object] = {}
    task_context: dict[str, object] = {}
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
    elif item.content_type is ContentType.SOCIAL_POST_PACK:
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
    else:
        raw_plan_item_id = (item.metadata_ or {}).get("publication_plan_item_id")
        try:
            plan_item_id = UUID(str(raw_plan_item_id))
        except (TypeError, ValueError):
            raise ContentAppError(
                "SOCIAL_POST_PLAN_CONTEXT_REQUIRED",
                "Для доработки поста не найден утверждённый пункт медиаплана.",
                409,
            ) from None
        plan_item = await session.scalar(
            select(PublicationPlanItem).where(PublicationPlanItem.id == plan_item_id)
        )
        plan = (
            await session.get(PublicationPlan, plan_item.publication_plan_id)
            if plan_item is not None
            else None
        )
        if (
            plan_item is None
            or plan is None
            or plan.status is not PublicationPlanStatus.APPROVED
            or item.channel != plan_item.channel
        ):
            raise ContentAppError(
                "SOCIAL_POST_PLAN_CONTEXT_INVALID",
                "Пункт медиаплана поста недоступен или больше не является утверждённым.",
                409,
            )
        current_version = await session.get(ContentVersion, item.current_version_id)
        if current_version is None:
            raise ContentAppError("CONTENT_VERSION_NOT_FOUND", "Текущая версия не найдена.", 409)
        article_version_ids = list(
            (
                await session.scalars(
                    select(ContentDerivation.source_content_version_id).where(
                        ContentDerivation.derived_content_version_id == current_version.id
                    )
                )
            ).all()
        )
        if plan_item.source_content_version_id not in article_version_ids:
            raise ContentAppError(
                "SOCIAL_POST_SOURCE_CONTEXT_INVALID",
                "Источник поста не совпадает с утверждённым медиапланом.",
                409,
            )
        immutable_context["article_version_ids"] = [
            str(value) for value in dict.fromkeys(article_version_ids)
        ]
        task_context = {
            "publication_plan_id": str(plan.id),
            "publication_plan_item_id": str(plan_item.id),
            "plan_channel": plan_item.channel.value,
            "scheduled_at": plan_item.scheduled_at.isoformat(),
            "source_content_item_id": str(plan_item.source_content_item_id),
            "source_content_version_id": str(plan_item.source_content_version_id),
            "source_claim_ids": plan_item.source_claim_ids or [],
            "source_support_summary": plan_item.source_support_summary,
            "topic": plan_item.topic,
            "angle": plan_item.angle,
            "purpose": plan_item.purpose,
            "format": plan_item.format,
            "message_brief": plan_item.message_brief,
            "original_text": current_version.content,
        }
    if approval is not None:
        approval.status = ApprovalStatus.REVISION_REQUESTED
        approval.reviewed_by_user_id = user.id
        approval.comment = comment
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
                **({"approval_id": str(approval.id)} if approval is not None else {}),
                **(
                    {"approved_base_approval_id": str(approved_base_approval.id)}
                    if approved_base_approval is not None
                    else {}
                ),
                "revision_comment": comment,
                "requested_by_user_id": str(user.id),
                "original_task_id": str(item.source_task_id),
                "revision_target_type": item.content_type.value,
                "immutable_source_context": immutable_context,
                **task_context,
            },
        ),
        commit=False,
    )
    task.optimization_action_id = optimization_action_id
    task.input_data = {**task.input_data, **(provenance or {})}
    await session.flush()
    await ActivityLogService(session).record(
        "CONTENT_REVISION_REQUESTED",
        operation_key=f"revision-request:{item.id}:{current_version_id}:{task.id}",
        campaign_id=item.campaign_id,
        task_id=task.id,
        user_id=user.id,
        content_item_id=item.id,
        approval_id=approval.id
        if approval
        else approved_base_approval.id
        if approved_base_approval
        else None,
        metadata={"revision_comment": comment},
    )
    return task
