from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.optimization import CampaignOptimizationAction, OptimizationActionStatus
from app.models.publication_plan import PublicationPlan
from app.models.task import Task, TaskStatus, TaskType
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
        recovered.extend(await self.recover_unenqueued_optimization_runs(limit=limit))
        return recovered

    async def recover_unenqueued_optimization_runs(self, *, limit: int = 50) -> list[AgentRun]:
        """Re-deliver durable applied work; never prepare a replacement artifact.

        A broker acknowledgement is not atomic with the database commit. Recovery
        may therefore deliver the same run twice; worker claims own execution.
        """
        plan_action = (
            select(PublicationPlan.optimization_action_id)
            .where(PublicationPlan.generated_by_agent_run_id == AgentRun.id)
            .correlate(AgentRun)
            .scalar_subquery()
        )
        applied_plan = (
            select(CampaignOptimizationAction.id)
            .where(
                CampaignOptimizationAction.id == plan_action,
                CampaignOptimizationAction.status == OptimizationActionStatus.APPLIED,
            )
            .exists()
        )
        applied_strategy = (
            select(CampaignOptimizationAction.id)
            .where(
                CampaignOptimizationAction.id == Task.optimization_action_id,
                CampaignOptimizationAction.status == OptimizationActionStatus.APPLIED,
            )
            .exists()
        )
        # Lock one run per transaction: enqueue commits release its lock. Selecting
        # a batch of locked rows would release the remaining rows prematurely.
        recovered: list[AgentRun] = []
        for _ in range(limit):
            run = await self.session.scalar(
                select(AgentRun)
                .join(Task, Task.id == AgentRun.task_id)
                .where(
                    AgentRun.status == AgentRunStatus.QUEUED,
                    AgentRun.queue_job_id.is_(None),
                    or_(
                        applied_plan,
                        (Task.task_type == TaskType.CAMPAIGN_PLANNING) & applied_strategy,
                    ),
                )
                .order_by(AgentRun.created_at, AgentRun.id)
                .with_for_update(of=AgentRun, skip_locked=True)
                .execution_options(populate_existing=True)
                .limit(1)
            )
            if run is None:
                await self.session.commit()
                break
            plan = await self.session.scalar(
                select(PublicationPlan).where(
                    PublicationPlan.generated_by_agent_run_id == run.id,
                    PublicationPlan.optimization_action_id.is_not(None),
                )
            )
            try:
                if plan is not None:
                    from app.services.publication_plan_service import PublicationPlanService

                    await PublicationPlanService(self.session).enqueue_generation(plan, run)
                else:
                    from app.services.agent_run_service import AgentRunService

                    await AgentRunService(self.session).enqueue(run)
            except AppError:
                # Enqueue already failed the existing run. Match the optimization
                # Apply failure path without changing ordinary dispatcher retries.
                task = await self.session.get(Task, run.task_id, with_for_update=True)
                if task is not None and run.status is AgentRunStatus.FAILED:
                    task.status = TaskStatus.FAILED
                    task.error_message = run.error_message
                    await self.session.commit()
                else:
                    raise
            recovered.append(run)
        return recovered
