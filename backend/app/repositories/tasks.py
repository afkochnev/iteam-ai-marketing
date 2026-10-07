from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.task import Task, TaskDependency, TaskPriority, TaskStatus, TaskType


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _load_options() -> tuple[Any, ...]:
        return (
            selectinload(Task.campaign),
            selectinload(Task.assigned_agent),
            selectinload(Task.parent_task),
            selectinload(Task.dependencies).selectinload(TaskDependency.depends_on_task),
            selectinload(Task.dependent_links).selectinload(TaskDependency.task),
        )

    async def list_tasks(
        self,
        *,
        campaign_id: UUID | None = None,
        agent_id: UUID | None = None,
        status: TaskStatus | None = None,
        task_type: TaskType | None = None,
        priority: TaskPriority | None = None,
    ) -> list[Task]:
        statement = select(Task).options(*self._load_options()).where(Task.is_internal.is_(False))
        for condition in (
            Task.campaign_id == campaign_id if campaign_id else None,
            Task.assigned_agent_id == agent_id if agent_id else None,
            Task.status == status if status else None,
            Task.task_type == task_type if task_type else None,
            Task.priority == priority if priority else None,
        ):
            if condition is not None:
                statement = statement.where(condition)
        result = await self.session.execute(
            statement.order_by(Task.created_at.desc(), Task.id.desc())
        )
        return list(result.scalars().unique().all())

    async def get_by_id(self, task_id: UUID) -> Task | None:
        result = await self.session.execute(
            select(Task).options(*self._load_options()).where(Task.id == task_id)
        )
        return result.scalar_one_or_none()

    async def create(self, values: Mapping[str, Any]) -> Task:
        task = Task(**values)
        self.session.add(task)
        await self.session.flush()
        return task

    async def update(self, task: Task, changes: Mapping[str, Any]) -> None:
        for field, value in changes.items():
            setattr(task, field, value)
        await self.session.flush()

    async def add_dependency(self, task_id: UUID, depends_on_task_id: UUID) -> TaskDependency:
        dependency = TaskDependency(task_id=task_id, depends_on_task_id=depends_on_task_id)
        self.session.add(dependency)
        await self.session.flush()
        return dependency

    async def remove_dependency(self, task_id: UUID, depends_on_task_id: UUID) -> bool:
        result = await self.session.execute(
            delete(TaskDependency).where(
                TaskDependency.task_id == task_id,
                TaskDependency.depends_on_task_id == depends_on_task_id,
            )
        )
        return bool(result.rowcount)

    async def dependency_ids(self, task_id: UUID) -> list[UUID]:
        result = await self.session.scalars(
            select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == task_id)
        )
        return list(result)

    async def dependency_edges(self, campaign_id: UUID) -> list[tuple[UUID, UUID]]:
        result = await self.session.execute(
            select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
            .join(Task, Task.id == TaskDependency.task_id)
            .where(Task.campaign_id == campaign_id)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def get_dependents(self, task_id: UUID) -> list[Task]:
        result = await self.session.scalars(
            select(Task)
            .join(TaskDependency, Task.id == TaskDependency.task_id)
            .where(TaskDependency.depends_on_task_id == task_id)
        )
        return list(result)
