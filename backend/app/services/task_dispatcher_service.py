from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.task import Task, TaskStatus, TaskType
from app.services.activity_log_service import ActivityLogService
from app.services.agent_run_service import AgentRunService
from app.services.retry_policy import classify_error
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


AUTO_TASK_TYPES = frozenset(
    {
        TaskType.KNOWLEDGE_RESEARCH,
        TaskType.WRITE_ARTICLE,
        TaskType.CREATE_SOCIAL_POSTS,
        TaskType.CONTENT_REVISION,
    }
)


class TaskDispatcherService:
    """Application-owned dispatcher for ready AI work.

    PostgreSQL row locking is the coordination primitive.  A competing
    dispatcher either skips a locked row or observes the active-run unique
    constraint and safely moves on; Celery is never used as a lock.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def dispatch_ready_tasks(self, *, limit: int = 20) -> list[UUID]:
        task_ids = list(
            (
                await self.session.scalars(
                    select(Task.id)
                    .where(
                        Task.status == TaskStatus.READY,
                        Task.is_internal.is_(False),
                        Task.task_type.in_(AUTO_TASK_TYPES),
                    )
                    .order_by(Task.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        dispatched: list[UUID] = []
        for task_id in task_ids:
            try:
                task = await self.session.get(Task, task_id, with_for_update=True)
                if task is not None and task.task_type is TaskType.CREATE_SOCIAL_POSTS:
                    approved_version_id = await TaskService(
                        self.session
                    ).approved_article_version_for_smm(task)
                    if approved_version_id is None:
                        task.status = TaskStatus.BLOCKED
                        await self.session.commit()
                        continue
                    task.input_data = {
                        **task.input_data,
                        "source_content_version_id": str(approved_version_id),
                    }
                run_service = AgentRunService(self.session)
                run = await run_service.create_queued_run(task_id)
                retry_task = await self.session.get(Task, task_id)
                retry_count = retry_task.retry_count if retry_task is not None else 0
                delay = classify_error("AGENT_TIMEOUT", max(0, retry_count - 1)).delay_seconds
                if retry_count:
                    await run_service.enqueue(run, countdown=delay)
                else:
                    await run_service.enqueue(run)
                task = await self.session.get(Task, task_id)
                if task:
                    await ActivityLogService(self.session).record(
                        "TASK_AUTO_DISPATCHED",
                        operation_key=f"dispatch:{run.id}",
                        campaign_id=task.campaign_id,
                        task_id=task.id,
                        agent_id=run.agent_id,
                        metadata={"agent_run_id": str(run.id)},
                    )
                    await self.session.commit()
                dispatched.append(task_id)
            except AppError as exc:
                # A concurrent dispatcher may have won the race.  This is a
                # normal outcome and must not stop the remaining batch.
                if exc.code not in {
                    "TASK_ALREADY_QUEUED_OR_RUNNING",
                    "TASK_NOT_READY",
                    "AGENT_INACTIVE",
                }:
                    logger.warning(
                        "Task dispatch skipped", extra={"task_id": str(task_id), "code": exc.code}
                    )
            except Exception:
                logger.exception("Task dispatch failed", extra={"task_id": str(task_id)})
        return dispatched

    async def dispatch(self, *, limit: int = 20) -> list[UUID]:
        return await self.dispatch_ready_tasks(limit=limit)
