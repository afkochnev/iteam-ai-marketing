from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun
from app.models.approval import Approval, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.agents import AgentRepository
from app.repositories.approvals import ApprovalRepository
from app.schemas.agent_outputs import CampaignPlan
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.approval_service import ApprovalService
from app.services.task_service import TaskService


class CampaignPlanningService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _locked_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = (
            await self.session.execute(
                select(Campaign).where(Campaign.id == campaign_id).with_for_update()
            )
        ).scalar_one_or_none()
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        return campaign

    async def _marketing_director(self) -> Agent:
        agent = await AgentRepository(self.session).get_by_slug("marketing_director")
        if agent is None or agent.status is not AgentStatus.ACTIVE:
            raise AppError(
                "MARKETING_DIRECTOR_NOT_AVAILABLE", "Marketing Director недоступен.", 409
            )
        return agent

    async def _enqueue_planning(self, task: Task) -> AgentRun:
        run_service = AgentRunService(self.session)
        run = await run_service.create_queued_run(task.id)
        try:
            return await run_service.enqueue(run)
        except AppError:
            current = await TaskService(self.session).get_task(task.id)
            current.status = TaskStatus.FAILED
            current.error_message = "Не удалось поставить планирование в очередь."
            await self.session.commit()
            raise

    async def generate(self, campaign_id: UUID) -> tuple[Campaign, Task, AgentRun]:
        campaign = await self._locked_campaign(campaign_id)
        if campaign.status is CampaignStatus.PLANNING:
            raise AppError("CAMPAIGN_ALREADY_PLANNING", "Стратегия уже формируется.", 409)
        if campaign.status is not CampaignStatus.DRAFT:
            raise AppError(
                "CAMPAIGN_NOT_DRAFT", "Стратегию можно создать только для черновика.", 409
            )
        agent = await self._marketing_director()
        campaign.status = CampaignStatus.PLANNING
        task = await TaskService(self.session).create_task(
            TaskCreate(
                campaign_id=campaign.id,
                task_type=TaskType.CAMPAIGN_PLANNING,
                title=f"Сформировать стратегию: {campaign.name}",
                description="Подготовить структурированный план маркетинговой кампании.",
                assigned_agent_id=agent.id,
                input_data={"strategy_version": campaign.strategy_version + 1},
            )
        )
        run = await self._enqueue_planning(task)
        return campaign, task, run

    async def approve(
        self, campaign_id: UUID, reviewer: User, comment: str | None
    ) -> tuple[Campaign, Approval, list[Task]]:
        campaign = await self._locked_campaign(campaign_id)
        if campaign.status is CampaignStatus.ACTIVE:
            approved = next(
                (
                    item
                    for item in await ApprovalRepository(self.session).list_approvals(
                        object_id=campaign.id
                    )
                    if item.status is ApprovalStatus.APPROVED
                ),
                None,
            )
            if approved:
                task_ids = [UUID(item) for item in approved.metadata_.get("generated_task_ids", [])]
                tasks = [
                    task
                    for task_id in task_ids
                    if (task := await TaskService(self.session).get_task(task_id))
                ]
                return campaign, approved, tasks
        if campaign.status is not CampaignStatus.WAITING_APPROVAL or not campaign.strategy:
            raise AppError(
                "CAMPAIGN_NOT_WAITING_APPROVAL", "Кампания не ожидает согласования.", 409
            )
        approval = await ApprovalService(self.session).get_pending(campaign.id, lock=True)
        plan = CampaignPlan.model_validate(approval.subject_snapshot)
        agents: dict[str, Agent] = {}
        for planned in plan.tasks:
            if planned.agent_slug in agents:
                continue
            agent = await AgentRepository(self.session).get_by_slug(planned.agent_slug)
            if agent is None:
                raise AppError(
                    "REQUIRED_AGENT_NOT_FOUND", f"Не найден агент {planned.agent_slug}.", 409
                )
            if agent.status is not AgentStatus.ACTIVE:
                raise AppError(
                    "REQUIRED_AGENT_INACTIVE", f"Агент {planned.agent_slug} неактивен.", 409
                )
            agents[planned.agent_slug] = agent
        created: dict[str, Task] = {}
        task_service = TaskService(self.session)
        pending = {item.key: item for item in plan.tasks}
        while pending:
            planned = next(
                item for item in pending.values() if all(key in created for key in item.depends_on)
            )
            dependencies = [created[key].id for key in planned.depends_on]
            created[planned.key] = await task_service.create_task(
                TaskCreate(
                    campaign_id=campaign.id,
                    task_type=planned.task_type,
                    title=planned.title,
                    description=planned.description,
                    assigned_agent_id=agents[planned.agent_slug].id,
                    priority=planned.priority,
                    input_data={
                        "strategy_version": campaign.strategy_version,
                        "plan_task_key": planned.key,
                        "brief": planned.brief,
                        "campaign_strategy_reference": {
                            "campaign_id": str(campaign.id),
                            "strategy_version": campaign.strategy_version,
                        },
                    },
                    dependency_ids=dependencies,
                ),
                commit=False,
            )
            del pending[planned.key]
        await ApprovalService(self.session).resolve(
            approval, ApprovalStatus.APPROVED, reviewer, comment
        )
        approval.metadata_ = {
            "generated_task_ids": [str(item.id) for item in created.values()],
            "generated_task_count": len(created),
        }
        campaign.status = CampaignStatus.ACTIVE
        await self.session.commit()
        return campaign, approval, list(created.values())

    async def reject(self, campaign_id: UUID, reviewer: User, comment: str) -> Campaign:
        campaign = await self._locked_campaign(campaign_id)
        if campaign.status is not CampaignStatus.WAITING_APPROVAL:
            raise AppError(
                "CAMPAIGN_NOT_WAITING_APPROVAL", "Кампания не ожидает согласования.", 409
            )
        approval = await ApprovalService(self.session).get_pending(campaign.id, lock=True)
        await ApprovalService(self.session).resolve(
            approval, ApprovalStatus.REJECTED, reviewer, comment
        )
        campaign.status = CampaignStatus.DRAFT
        await self.session.commit()
        return campaign

    async def request_revision(
        self, campaign_id: UUID, reviewer: User, comment: str
    ) -> tuple[Campaign, Task, AgentRun]:
        campaign = await self._locked_campaign(campaign_id)
        if campaign.status is not CampaignStatus.WAITING_APPROVAL or not campaign.strategy:
            raise AppError(
                "CAMPAIGN_NOT_WAITING_APPROVAL", "Кампания не ожидает согласования.", 409
            )
        approval = await ApprovalService(self.session).get_pending(campaign.id, lock=True)
        agent = await self._marketing_director()
        await ApprovalService(self.session).resolve(
            approval, ApprovalStatus.REVISION_REQUESTED, reviewer, comment
        )
        campaign.status = CampaignStatus.PLANNING
        task = await TaskService(self.session).create_task(
            TaskCreate(
                campaign_id=campaign.id,
                task_type=TaskType.CAMPAIGN_PLANNING,
                title=f"Доработать стратегию v{campaign.strategy_version + 1}",
                description="Учесть комментарий человека и подготовить новую версию.",
                assigned_agent_id=agent.id,
                input_data={
                    "strategy_version": campaign.strategy_version + 1,
                    "revision": True,
                    "reviewer_feedback": comment,
                    "previous_strategy": campaign.strategy,
                },
            )
        )
        run = await self._enqueue_planning(task)
        return campaign, task, run
