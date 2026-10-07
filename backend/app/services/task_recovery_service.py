from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.task import Task, TaskStatus
from app.services.activity_log_service import ActivityLogService


class TaskRecoveryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def recover_stuck(self, *, limit: int = 50) -> list[AgentRun]:
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds)
        runs = list(
            (
                await self.session.scalars(
                    select(AgentRun)
                    .join(Task, Task.id == AgentRun.task_id)
                    .where(
                        AgentRun.status == AgentRunStatus.RUNNING,
                        Task.status == TaskStatus.IN_PROGRESS,
                        AgentRun.started_at.is_not(None),
                        AgentRun.started_at < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        recovered: list[AgentRun] = []
        for run in runs:
            task = await self.session.get(Task, run.task_id, with_for_update=True)
            if task is None or task.status is not TaskStatus.IN_PROGRESS:
                continue
            run.status = AgentRunStatus.FAILED
            run.error_code = "AGENT_STUCK"
            run.error_message = "Выполнение агента превысило допустимое время ожидания."
            run.completed_at = datetime.now(UTC)
            task.status = TaskStatus.FAILED
            task.error_message = run.error_message
            task.completed_at = run.completed_at
            await ActivityLogService(self.session).record(
                "TASK_RECOVERED_FROM_STUCK_RUN",
                operation_key=f"stuck-recovered:{run.id}",
                campaign_id=task.campaign_id,
                task_id=task.id,
                agent_id=run.agent_id,
                metadata={"agent_run_id": str(run.id), "error_code": run.error_code},
            )
            from app.core.director_chat import is_director_chat

            if is_director_chat(task):
                from app.services.director_chat_service import fail_chat_response

                await fail_chat_response(self.session, run, "AGENT_STUCK")
            recovered.append(run)
        await self.session.commit()
        return recovered
