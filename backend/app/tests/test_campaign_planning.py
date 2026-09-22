import asyncio
from collections.abc import Callable
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus
from app.models.approval import ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.approvals import ApprovalRepository
from app.repositories.users import UserRepository
from app.schemas.agent_outputs import CampaignPlan
from app.schemas.campaign import CampaignCreate
from app.services.agent_run_service import AgentRunService, build_task_input
from app.services.agent_runner_service import RuntimeResult
from app.services.campaign_planning_service import CampaignPlanningService
from app.services.campaign_service import CampaignService
from app.services.task_service import TaskService


def plan_data() -> dict[str, object]:
    return {
        "campaign_summary": "Экспертная кампания",
        "positioning": "Диагностика как первый шаг",
        "target_audience": "Собственники и CEO",
        "main_message": "Верните управляемость росту",
        "content_strategy": "Экспертная статья и серия постов",
        "content_topics": ["Рост", "Управляемость", "Диагностика"],
        "recommended_article": {
            "title": "Как вернуть управляемость",
            "objective": "Объяснить проблему",
            "angle": "Через симптомы роста",
            "cta": "Пройти диагностику",
        },
        "social_strategy": {
            "channels": ["TELEGRAM", "VK"],
            "post_count": 5,
            "approach": "Раскрывать тезисы статьи",
        },
        "tasks": [
            {
                "key": "research",
                "task_type": "KNOWLEDGE_RESEARCH",
                "title": "Найти материалы",
                "description": "Исследовать базу знаний",
                "agent_slug": "knowledge_keeper",
                "priority": "HIGH",
                "brief": "Найти релевантные источники",
                "depends_on": [],
            },
            {
                "key": "article",
                "task_type": "WRITE_ARTICLE",
                "title": "Написать статью",
                "description": "Создать основную статью",
                "agent_slug": "writer",
                "priority": "NORMAL",
                "brief": "Написать экспертную статью",
                "depends_on": ["research"],
            },
            {
                "key": "social",
                "task_type": "CREATE_SOCIAL_POSTS",
                "title": "Создать публикации",
                "description": "Подготовить пакет публикаций",
                "agent_slug": "smm_manager",
                "priority": "NORMAL",
                "brief": "Создать пять постов",
                "depends_on": ["article"],
            },
        ],
    }


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data: data["tasks"].append(deepcopy(data["tasks"][0])),
        lambda data: data["tasks"][1]["depends_on"].append("missing"),
        lambda data: data["tasks"][0]["depends_on"].append("research"),
        lambda data: data["tasks"][0]["depends_on"].append("social"),
        lambda data: data["tasks"][1].update({"task_type": "MANUAL"}),
        lambda data: data["tasks"][1].update({"agent_slug": "knowledge_keeper"}),
        lambda data: data.update({"tasks": data["tasks"][1:]}),
        lambda data: data["tasks"][1].update({"depends_on": []}),
        lambda data: data["tasks"][2].update({"depends_on": ["research"]}),
        lambda data: data["social_strategy"].update({"post_count": 4}),
        lambda data: data["social_strategy"].update({"post_count": 11}),
    ],
)
def test_campaign_plan_rejects_invalid_graph(
    mutator: Callable[[dict[str, object]], object],
) -> None:
    data = plan_data()
    mutator(data)
    with pytest.raises(ValidationError):
        CampaignPlan.model_validate(data)


async def setup_campaign(session: AsyncSession) -> tuple[User, Campaign]:
    user = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Reviewer",
        role=UserRole.ADMIN,
    )
    for slug in ("marketing_director", "knowledge_keeper", "writer", "smm_manager"):
        session.add(
            Agent(
                name=slug,
                slug=slug,
                role=slug,
                system_prompt="Plan the campaign",
                model="test-model",
                status=AgentStatus.ACTIVE,
                autonomy_level=3 if slug == "marketing_director" else 2,
                settings={},
            )
        )
    await session.commit()
    campaign = await CampaignService(session).create_campaign(
        CampaignCreate(name="Campaign", goal="30 leads", target_audience="CEO"), user
    )
    return user, campaign


async def complete_plan(session: AsyncSession, campaign_id: UUID) -> tuple[Campaign, Task]:
    planning = CampaignPlanningService(session)
    campaign, task, run = await planning.generate(campaign_id)
    service = AgentRunService(session)
    claimed = await service.claim(run.id)
    assert claimed is not None
    await service.finish_success(run.id, RuntimeResult(plan_data(), 1, 100, 50, 150, None))
    return campaign, task


async def test_planning_approval_creates_graph(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: SimpleNamespace(id="job"),
    )
    user, campaign = await setup_campaign(db_session)
    _, planning_task = await complete_plan(db_session, campaign.id)
    current = await CampaignService(db_session).get_campaign(campaign.id)
    assert current.status is CampaignStatus.WAITING_APPROVAL
    assert current.strategy_version == 1 and current.strategy is not None
    pending = await ApprovalRepository(db_session).current_pending(current.id)
    assert pending is not None and pending.subject_snapshot == plan_data()
    planning_row = await TaskService(db_session).get_task(planning_task.id)
    assert planning_row.status is TaskStatus.COMPLETED

    writer = (await db_session.execute(select(Agent).where(Agent.slug == "writer"))).scalar_one()
    writer.status = AgentStatus.INACTIVE
    await db_session.commit()
    with pytest.raises(AppError) as inactive:
        await CampaignPlanningService(db_session).approve(current.id, user, None)
    assert inactive.value.code == "REQUIRED_AGENT_INACTIVE"
    assert (await ApprovalRepository(db_session).current_pending(current.id)) is not None
    writer.status = AgentStatus.ACTIVE
    await db_session.commit()
    _, approval, tasks = await CampaignPlanningService(db_session).approve(
        current.id, user, "Согласовано"
    )
    assert approval.status is ApprovalStatus.APPROVED
    assert (
        await CampaignService(db_session).get_campaign(current.id)
    ).status is CampaignStatus.ACTIVE
    by_type = {task.task_type: await TaskService(db_session).get_task(task.id) for task in tasks}
    assert by_type[TaskType.KNOWLEDGE_RESEARCH].status is TaskStatus.READY
    assert by_type[TaskType.WRITE_ARTICLE].status is TaskStatus.BLOCKED
    assert by_type[TaskType.CREATE_SOCIAL_POSTS].status is TaskStatus.BLOCKED
    _, repeated, repeated_tasks = await CampaignPlanningService(db_session).approve(
        current.id, user, None
    )
    assert repeated.id == approval.id and {item.id for item in repeated_tasks} == {
        item.id for item in tasks
    }


async def test_revision_rejection_and_runtime_input(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: SimpleNamespace(id="job"),
    )
    user, campaign = await setup_campaign(db_session)
    await complete_plan(db_session, campaign.id)
    current, task, _ = await CampaignPlanningService(db_session).request_revision(
        campaign.id, user, "Усилить фокус"
    )
    assert current.status is CampaignStatus.PLANNING
    assert "Усилить фокус" in build_task_input(await TaskService(db_session).get_task(task.id))
    history = await ApprovalRepository(db_session).list_approvals(object_id=campaign.id)
    assert history[0].status is ApprovalStatus.REVISION_REQUESTED
    run = await AgentRunService(db_session).repository.get_active_for_task(task.id)
    assert run is not None and await AgentRunService(db_session).claim(run.id)
    revised = plan_data()
    revised["main_message"] = "Новая версия"
    await AgentRunService(db_session).finish_success(
        run.id, RuntimeResult(revised, 1, 10, 10, 20, None)
    )
    after = await CampaignService(db_session).get_campaign(campaign.id)
    assert after.strategy_version == 2 and after.status is CampaignStatus.WAITING_APPROVAL
    await CampaignPlanningService(db_session).reject(after.id, user, "Не подходит")
    assert (await CampaignService(db_session).get_campaign(after.id)).status is CampaignStatus.DRAFT


async def test_inactive_agents_and_cancellation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: SimpleNamespace(id="job"),
    )
    _user, campaign = await setup_campaign(db_session)
    director = (
        await db_session.execute(select(Agent).where(Agent.slug == "marketing_director"))
    ).scalar_one()
    director.status = AgentStatus.INACTIVE
    await db_session.commit()
    with pytest.raises(AppError) as unavailable:
        await CampaignPlanningService(db_session).generate(campaign.id)
    assert unavailable.value.code == "MARKETING_DIRECTOR_NOT_AVAILABLE"
    director.status = AgentStatus.ACTIVE
    await db_session.commit()
    _, task, _ = await CampaignPlanningService(db_session).generate(campaign.id)
    await TaskService(db_session).cancel_task(task.id)
    assert (
        await CampaignService(db_session).get_campaign(campaign.id)
    ).status is CampaignStatus.DRAFT


async def test_campaign_planning_api(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: SimpleNamespace(id="job"),
    )
    user, campaign = await setup_campaign(db_session)
    assert (
        await client.post(f"/api/v1/campaigns/{campaign.id}/generate-strategy")
    ).status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "password"}
    )
    assert login.status_code == 200
    generated = await client.post(f"/api/v1/campaigns/{campaign.id}/generate-strategy")
    assert generated.status_code == 202 and generated.json()["status"] == "PLANNING"
    assert (
        await client.post(f"/api/v1/campaigns/{campaign.id}/generate-strategy")
    ).status_code == 409
    run_id = UUID(generated.json()["agent_run_id"])
    service = AgentRunService(db_session)
    assert await service.claim(run_id)
    await service.finish_success(run_id, RuntimeResult(plan_data(), 1, 10, 10, 20, None))
    approvals = await client.get(
        f"/api/v1/approvals?object_type=CAMPAIGN_STRATEGY&object_id={campaign.id}"
    )
    assert approvals.status_code == 200 and approvals.json()[0]["status"] == "PENDING"
    approved = await client.post(
        f"/api/v1/campaigns/{campaign.id}/approve-strategy", json={"comment": "OK"}
    )
    assert approved.status_code == 200
    assert approved.json()["campaign"]["status"] == "ACTIVE"
    assert len(approved.json()["generated_task_ids"]) == 3


async def test_concurrent_approval_creates_one_graph(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: SimpleNamespace(id="job"),
    )
    user, campaign = await setup_campaign(db_session)
    await complete_plan(db_session, campaign.id)

    async def approve_once() -> set[UUID]:
        async with async_session_factory() as session:
            _, _, tasks = await CampaignPlanningService(session).approve(campaign.id, user, None)
            return {task.id for task in tasks}

    first, second = await asyncio.gather(approve_once(), approve_once())
    assert first == second and len(first) == 3
    generated = [
        task
        for task in await TaskService(db_session).list_tasks(campaign_id=campaign.id)
        if task.task_type is not TaskType.CAMPAIGN_PLANNING
    ]
    assert len(generated) == 3


async def test_invalid_plan_then_planning_retry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: SimpleNamespace(id="job"),
    )
    _, campaign = await setup_campaign(db_session)
    _, task, run = await CampaignPlanningService(db_session).generate(campaign.id)
    service = AgentRunService(db_session)
    assert await service.claim(run.id)
    await service.finish_success(run.id, RuntimeResult({"invalid": True}, 1, 1, 1, 2, None))
    failed_task = await TaskService(db_session).get_task(task.id)
    assert failed_task.status is TaskStatus.FAILED
    assert (
        await CampaignService(db_session).get_campaign(campaign.id)
    ).status is CampaignStatus.PLANNING
    assert await ApprovalRepository(db_session).current_pending(campaign.id) is None
    retry = await service.create_queued_run(task.id, retry=True)
    assert await service.claim(retry.id)
    await service.finish_success(retry.id, RuntimeResult(plan_data(), 1, 10, 10, 20, None))
    current = await CampaignService(db_session).get_campaign(campaign.id)
    assert current.strategy_version == 1 and current.status is CampaignStatus.WAITING_APPROVAL
