from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent import Agent
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem, ContentStatus, ContentType, ContentVersion
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.repositories.tasks import TaskRepository
from app.schemas.task import TaskCreate, TaskUpdate
from app.services.plan_item_smm_service import PlanItemSmmContext, PlanItemSmmService


class TaskService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = TaskRepository(session)

    async def list_tasks(
        self,
        *,
        campaign_id: UUID | None = None,
        agent_id: UUID | None = None,
        status: TaskStatus | None = None,
        task_type: TaskType | None = None,
        priority: TaskPriority | None = None,
    ) -> list[Task]:
        return await self.repository.list_tasks(
            campaign_id=campaign_id,
            agent_id=agent_id,
            status=status,
            task_type=task_type,
            priority=priority,
        )

    async def get_task(self, task_id: UUID) -> Task:
        task = await self.repository.get_by_id(task_id)
        if task is None:
            raise AppError("TASK_NOT_FOUND", "Задача не найдена.", 404)
        return task

    async def create_task(self, payload: TaskCreate, *, commit: bool = True) -> Task:
        if payload.task_type is TaskType.ANALYZE_PERFORMANCE:
            raise AppError(
                "ANALYSIS_PREPARATION_REQUIRED", "Используйте подготовку анализа кампании.", 422
            )
        campaign = await self._get_campaign(payload.campaign_id)
        self._ensure_campaign_editable(campaign)
        if (
            payload.assigned_agent_id
            and await self.session.get(Agent, payload.assigned_agent_id) is None
        ):
            raise AppError("AGENT_NOT_FOUND", "Агент не найден.", 404)
        if payload.parent_task_id:
            parent = await self.get_task(payload.parent_task_id)
            if parent.campaign_id != payload.campaign_id:
                raise AppError(
                    "TASK_CROSS_CAMPAIGN_PARENT",
                    "Родительская задача должна принадлежать той же кампании.",
                    409,
                )
        plan_context: PlanItemSmmContext | None = None
        if payload.task_type is TaskType.CREATE_SOCIAL_POSTS and payload.input_data.get(
            "publication_plan_item_id"
        ):
            plan_context = await PlanItemSmmService(self.session).validate(
                payload.campaign_id,
                payload.input_data,
                assigned_agent_id=payload.assigned_agent_id,
            )
            existing = await self._active_plan_item_task(plan_context.item.id)
            if existing is not None:
                existing.input_data = {**existing.input_data, **plan_context.task_input()}
                if existing.status is TaskStatus.BLOCKED:
                    existing.status = TaskStatus.READY
                    existing.error_message = None
                if commit:
                    await self.session.commit()
                else:
                    await self.session.flush()
                return await self.get_task(existing.id)
        dependency_ids = list(dict.fromkeys(payload.dependency_ids))
        if len(dependency_ids) != len(payload.dependency_ids):
            raise AppError("TASK_DEPENDENCY_ALREADY_EXISTS", "Зависимость указана повторно.", 409)
        dependencies = [await self.get_task(item) for item in dependency_ids]
        if any(item.campaign_id != payload.campaign_id for item in dependencies):
            raise AppError(
                "TASK_CROSS_CAMPAIGN_DEPENDENCY",
                "Задачи из разных кампаний нельзя связывать.",
                409,
            )
        values = payload.model_dump(exclude={"dependency_ids"})
        if plan_context is not None:
            values["input_data"] = {**payload.input_data, **plan_context.task_input()}
        values["status"] = (
            TaskStatus.READY
            if all(item.status is TaskStatus.COMPLETED for item in dependencies)
            else TaskStatus.BLOCKED
        )
        values["output_data"] = {}
        values["retry_count"] = 0
        task = await self.repository.create(values)
        for dependency_id in dependency_ids:
            await self.repository.add_dependency(task.id, dependency_id)
        await self._resolve_status(task)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return await self.get_task(task.id)

    async def update_task(self, task_id: UUID, payload: TaskUpdate) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status in {
            TaskStatus.IN_PROGRESS,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
        }:
            raise AppError(
                "TASK_NOT_EDITABLE",
                "Задачу в текущем статусе нельзя редактировать.",
                409,
            )
        changes = payload.model_dump(exclude_unset=True)
        if task.task_type is TaskType.ANALYZE_PERFORMANCE and (
            {"assigned_agent_id", "input_data"} & changes.keys()
        ):
            raise AppError("ANALYSIS_TASK_IMMUTABLE", "Evidence и агент анализа неизменяемы.", 409)
        agent_id = changes.get("assigned_agent_id")
        if agent_id and await self.session.get(Agent, agent_id) is None:
            raise AppError("AGENT_NOT_FOUND", "Агент не найден.", 404)
        await self.repository.update(task, changes)
        await self.session.commit()
        return await self.get_task(task.id)

    async def add_dependency(self, task_id: UUID, depends_on_task_id: UUID) -> Task:
        task = await self.get_task(task_id)
        dependency = await self.get_task(depends_on_task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status in {
            TaskStatus.IN_PROGRESS,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
        }:
            raise AppError("TASK_NOT_EDITABLE", "Зависимости этой задачи нельзя изменять.", 409)
        if task.id == dependency.id:
            raise AppError("TASK_SELF_DEPENDENCY", "Задача не может зависеть от самой себя.", 409)
        if task.campaign_id != dependency.campaign_id:
            raise AppError(
                "TASK_CROSS_CAMPAIGN_DEPENDENCY",
                "Задачи из разных кампаний нельзя связывать.",
                409,
            )
        if dependency.id in await self.repository.dependency_ids(task.id):
            raise AppError(
                "TASK_DEPENDENCY_ALREADY_EXISTS",
                "Такая зависимость уже существует.",
                409,
            )
        if await self._would_create_cycle(task, dependency):
            raise AppError("TASK_DEPENDENCY_CYCLE", "Зависимость создаёт цикл.", 409)
        await self.repository.add_dependency(task.id, dependency.id)
        await self._resolve_status(task)
        await self.session.commit()
        return await self.get_task(task.id)

    async def remove_dependency(self, task_id: UUID, dependency_id: UUID) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status in {
            TaskStatus.IN_PROGRESS,
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
        }:
            raise AppError("TASK_NOT_EDITABLE", "Зависимости этой задачи нельзя изменять.", 409)
        if not await self.repository.remove_dependency(task.id, dependency_id):
            raise AppError("TASK_DEPENDENCY_NOT_FOUND", "Зависимость не найдена.", 404)
        await self._resolve_status(task)
        await self.session.commit()
        return await self.get_task(task.id)

    async def start_task(self, task_id: UUID) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status is not TaskStatus.READY:
            raise AppError("TASK_NOT_READY", "Начать можно только готовую задачу.", 409)
        await self.repository.update(
            task, {"status": TaskStatus.IN_PROGRESS, "started_at": datetime.now(UTC)}
        )
        await self.session.commit()
        return await self.get_task(task.id)

    async def complete_task(
        self, task_id: UUID, output_data: dict[str, object], *, commit: bool = True
    ) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status is not TaskStatus.IN_PROGRESS:
            raise AppError(
                "INVALID_TASK_TRANSITION",
                "Завершить можно только задачу в работе.",
                409,
            )
        await self.repository.update(
            task,
            {
                "status": TaskStatus.COMPLETED,
                "completed_at": datetime.now(UTC),
                "output_data": output_data,
            },
        )
        for dependent in await self.repository.get_dependents(task.id):
            if dependent.status is TaskStatus.BLOCKED:
                await self._resolve_status(dependent)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return await self.get_task(task.id)

    async def cancel_task(self, task_id: UUID) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status not in {
            TaskStatus.READY,
            TaskStatus.BLOCKED,
            TaskStatus.IN_PROGRESS,
        }:
            raise AppError(
                "INVALID_TASK_TRANSITION",
                "Задачу в текущем статусе нельзя отменить.",
                409,
            )
        await self.repository.update(task, {"status": TaskStatus.CANCELLED})
        if (
            task.task_type is TaskType.CAMPAIGN_PLANNING
            and task.campaign.status is CampaignStatus.PLANNING
            and task.input_data.get("strategy_version") == task.campaign.strategy_version + 1
        ):
            task.campaign.status = (
                CampaignStatus.ACTIVE
                if task.input_data.get("preserve_active_strategy")
                else CampaignStatus.DRAFT
            )
        await self.session.commit()
        return await self.get_task(task.id)

    async def _resolve_status(self, task: Task) -> None:
        plan_bound = bool(
            task.task_type is TaskType.CREATE_SOCIAL_POSTS
            and task.input_data.get("publication_plan_item_id")
        )
        if plan_bound:
            context = await PlanItemSmmService(self.session).validate(
                task.campaign_id,
                task.input_data,
                assigned_agent_id=task.assigned_agent_id,
            )
            task.input_data = {**task.input_data, **context.task_input()}
            status = TaskStatus.READY
        else:
            dependency_ids = await self.repository.dependency_ids(task.id)
            dependencies = [await self.get_task(item) for item in dependency_ids]
            status = (
                TaskStatus.READY
                if all(item.status is TaskStatus.COMPLETED for item in dependencies)
                else TaskStatus.BLOCKED
            )
        if status is TaskStatus.READY and task.task_type is TaskType.CREATE_SOCIAL_POSTS:
            source_version_id = await self.approved_article_version_for_smm(task)
            if source_version_id is None:
                status = TaskStatus.BLOCKED
            else:
                task.input_data = {
                    **task.input_data,
                    "source_content_version_id": str(source_version_id),
                }
        await self.repository.update(task, {"status": status})

    async def approved_article_version_for_smm(self, task: Task) -> UUID | None:
        """Return the exact approved Article Version allowed for this SMM task."""

        if task.task_type is not TaskType.CREATE_SOCIAL_POSTS:
            return None
        if task.input_data.get("publication_plan_item_id"):
            try:
                context = await PlanItemSmmService(self.session).validate(
                    task.campaign_id,
                    task.input_data,
                    assigned_agent_id=task.assigned_agent_id,
                )
            except AppError:
                return None
            return context.version.id
        dependency_ids = await self.repository.dependency_ids(task.id)
        article_tasks = [
            item
            for item in [await self.get_task(item_id) for item_id in dependency_ids]
            if item.task_type is TaskType.WRITE_ARTICLE
            and item.status is TaskStatus.COMPLETED
            and item.campaign_id == task.campaign_id
        ]
        if len(article_tasks) != 1:
            return None
        article_task = article_tasks[0]
        raw_version_id = task.input_data.get("source_content_version_id") or (
            article_task.output_data.get("content_version_id")
        )
        if not raw_version_id:
            return None
        try:
            version_id = UUID(str(raw_version_id))
        except (TypeError, ValueError):
            return None
        value = await self.session.scalar(
            select(ContentVersion.id)
            .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
            .join(
                Approval,
                and_(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id == ContentItem.id,
                    Approval.subject_version == ContentVersion.version_number,
                    Approval.status == ApprovalStatus.APPROVED,
                ),
            )
            .where(
                ContentVersion.id == version_id,
                ContentItem.campaign_id == task.campaign_id,
                ContentItem.content_type == ContentType.ARTICLE,
                ContentItem.status != ContentStatus.ARCHIVED,
                ContentItem.source_task_id == article_task.id,
            )
        )
        return UUID(str(value)) if value is not None else None

    async def _active_plan_item_task(self, plan_item_id: UUID) -> Task | None:
        return cast(
            Task | None,
            await self.session.scalar(
                select(Task)
                .where(
                    Task.task_type == TaskType.CREATE_SOCIAL_POSTS,
                    Task.input_data["publication_plan_item_id"].astext == str(plan_item_id),
                    Task.status.in_(
                        {
                            TaskStatus.NEW,
                            TaskStatus.BLOCKED,
                            TaskStatus.READY,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.WAITING_REVIEW,
                            TaskStatus.WAITING_APPROVAL,
                        }
                    ),
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            ),
        )

    async def refresh_dependents_for_content(self, content_id: UUID) -> None:
        item = await self.session.get(ContentItem, content_id)
        if item is None or item.content_type is not ContentType.ARTICLE:
            return
        for dependent in await self.repository.get_dependents(item.source_task_id):
            if (
                dependent.task_type is TaskType.CREATE_SOCIAL_POSTS
                and dependent.status is TaskStatus.BLOCKED
            ):
                await self._resolve_status(dependent)

    async def _would_create_cycle(self, task: Task, dependency: Task) -> bool:
        graph: dict[UUID, set[UUID]] = {}
        for source, target in await self.repository.dependency_edges(task.campaign_id):
            graph.setdefault(source, set()).add(target)
        stack = [dependency.id]
        visited: set[UUID] = set()
        while stack:
            current = stack.pop()
            if current == task.id:
                return True
            if current not in visited:
                visited.add(current)
                stack.extend(graph.get(current, set()))
        return False

    async def _get_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = await self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        return campaign

    @staticmethod
    def _ensure_campaign_editable(campaign: Campaign) -> None:
        if campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED",
                "Архивная кампания доступна только для чтения.",
                409,
            )
