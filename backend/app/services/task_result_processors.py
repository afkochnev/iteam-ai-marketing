from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent_run import AgentRun
from app.models.campaign import Campaign, CampaignStatus
from app.models.task import Task, TaskType
from app.schemas.agent_outputs import CampaignPlan
from app.services.approval_service import ApprovalService
from app.services.task_service import TaskService


class TaskResultProcessor(Protocol):
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None: ...


class DefaultTaskResultProcessor:
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None:
        await TaskService(session).complete_task(task.id, output, commit=False)


class CampaignPlanningResultProcessor:
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None:
        try:
            plan = CampaignPlan.model_validate(output)
        except ValidationError as exc:
            raise AppError(
                "INVALID_CAMPAIGN_PLAN", "Структура стратегии не прошла проверку.", 422
            ) from exc
        campaign = (
            await session.execute(
                select(Campaign).where(Campaign.id == task.campaign_id).with_for_update()
            )
        ).scalar_one()
        target_version = int(task.input_data["strategy_version"])
        if (
            campaign.status is not CampaignStatus.PLANNING
            or target_version != campaign.strategy_version + 1
        ):
            raise AppError("INVALID_CAMPAIGN_PLAN", "Версия стратегии больше не актуальна.", 409)
        snapshot = plan.model_dump(mode="json")
        campaign.strategy = snapshot
        campaign.strategy_version = target_version
        campaign.status = CampaignStatus.WAITING_APPROVAL
        await ApprovalService(session).create_strategy_approval(
            campaign.id, target_version, snapshot, run.agent_id
        )
        await TaskService(session).complete_task(task.id, snapshot, commit=False)


class TaskResultProcessorRegistry:
    def get(self, task_type: TaskType) -> TaskResultProcessor:
        if task_type is TaskType.CAMPAIGN_PLANNING:
            return CampaignPlanningResultProcessor()
        return DefaultTaskResultProcessor()


result_processor_registry = TaskResultProcessorRegistry()
