from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent import Agent
from app.models.campaign import Campaign, CampaignStatus
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.repositories.tasks import TaskRepository
from app.schemas.task import TaskCreate, TaskUpdate


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

    async def create_task(self, payload: TaskCreate) -> Task:
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
        dependency_ids = list(dict.fromkeys(payload.dependency_ids))
        if len(dependency_ids) != len(payload.dependency_ids):
            raise AppError("TASK_DEPENDENCY_ALREADY_EXISTS", "Зависимость указана повторно.", 409)
        dependencies = [await self.get_task(item) for item in dependency_ids]
        if any(item.campaign_id != payload.campaign_id for item in dependencies):
            raise AppError(
                "TASK_CROSS_CAMPAIGN_DEPENDENCY", "Задачи из разных кампаний нельзя связывать.", 409
            )
        values = payload.model_dump(exclude={"dependency_ids"})
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
        await self.session.commit()
        return await self.get_task(task.id)

    async def update_task(self, task_id: UUID, payload: TaskUpdate) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            raise AppError(
                "TASK_NOT_EDITABLE", "Задачу в текущем статусе нельзя редактировать.", 409
            )
        changes = payload.model_dump(exclude_unset=True)
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
        if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            raise AppError("TASK_NOT_EDITABLE", "Зависимости этой задачи нельзя изменять.", 409)
        if task.id == dependency.id:
            raise AppError("TASK_SELF_DEPENDENCY", "Задача не может зависеть от самой себя.", 409)
        if task.campaign_id != dependency.campaign_id:
            raise AppError(
                "TASK_CROSS_CAMPAIGN_DEPENDENCY", "Задачи из разных кампаний нельзя связывать.", 409
            )
        if dependency.id in await self.repository.dependency_ids(task.id):
            raise AppError(
                "TASK_DEPENDENCY_ALREADY_EXISTS", "Такая зависимость уже существует.", 409
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
        if task.status in {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
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

    async def complete_task(self, task_id: UUID, output_data: dict[str, object]) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status is not TaskStatus.IN_PROGRESS:
            raise AppError(
                "INVALID_TASK_TRANSITION", "Завершить можно только задачу в работе.", 409
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
        await self.session.commit()
        return await self.get_task(task.id)

    async def cancel_task(self, task_id: UUID) -> Task:
        task = await self.get_task(task_id)
        self._ensure_campaign_editable(task.campaign)
        if task.status not in {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.IN_PROGRESS}:
            raise AppError(
                "INVALID_TASK_TRANSITION", "Задачу в текущем статусе нельзя отменить.", 409
            )
        await self.repository.update(task, {"status": TaskStatus.CANCELLED})
        await self.session.commit()
        return await self.get_task(task.id)

    async def _resolve_status(self, task: Task) -> None:
        dependency_ids = await self.repository.dependency_ids(task.id)
        dependencies = [await self.get_task(item) for item in dependency_ids]
        status = (
            TaskStatus.READY
            if all(item.status is TaskStatus.COMPLETED for item in dependencies)
            else TaskStatus.BLOCKED
        )
        await self.repository.update(task, {"status": status})

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
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
