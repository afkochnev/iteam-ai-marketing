from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.factory import AgentRuntimeContext, AgentSnapshot
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRunStatus
from app.models.campaign import Campaign
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.schemas.agent_outputs import CampaignPlan
from app.schemas.campaign import CampaignCreate
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import (
    AgentRunnerService,
    AgentRuntimeError,
    RuntimeResult,
)
from app.services.campaign_service import CampaignService
from app.services.task_service import TaskService
from app.tests.test_campaign_planning import plan_data


async def runtime_fixture(
    session: AsyncSession, *, task_type: TaskType = TaskType.MANUAL
) -> tuple[User, Agent, Campaign, Task]:
    owner = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Owner",
        role=UserRole.ADMIN,
    )
    agent = Agent(
        name="Runtime Agent",
        slug=f"runtime-{uuid4()}",
        role="test",
        system_prompt="Runtime prompt",
        model=None,
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    session.add(agent)
    await session.commit()
    campaign = await CampaignService(session).create_campaign(
        CampaignCreate(name="Runtime Campaign", goal="Test runtime"), owner
    )
    task = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            title="Runtime task",
            task_type=task_type,
            assigned_agent_id=agent.id,
        )
    )
    return owner, agent, campaign, task


async def test_queued_snapshot_duplicate_and_protected_type(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, agent, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert run.status is AgentRunStatus.QUEUED
    assert run.model == "test-model"
    assert run.prompt_snapshot == agent.system_prompt
    assert len(run.prompt_hash) == 64
    with pytest.raises(AppError) as duplicate:
        await service.create_queued_run(task.id)
    assert duplicate.value.code == "TASK_ALREADY_QUEUED_OR_RUNNING"
    _, _, _, unsupported = await runtime_fixture(db_session, task_type=TaskType.WRITE_ARTICLE)
    with pytest.raises(AppError) as error:
        await service.create_queued_run(unsupported.id)
    assert error.value.code == "INVALID_AGENT_FOR_TASK_TYPE"


async def test_success_unblocks_dependency(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, campaign, task = await runtime_fixture(db_session)
    downstream = await TaskService(db_session).create_task(
        TaskCreate(campaign_id=campaign.id, title="Downstream", dependency_ids=[task.id])
    )
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    claimed = await service.claim(run.id)
    assert (
        claimed is not None
        and (await TaskService(db_session).get_task(task.id)).status is TaskStatus.IN_PROGRESS
    )
    await service.finish_success(
        run.id, RuntimeResult({"text": "Тестовый результат агента"}, 1, 10, 5, 15, None)
    )
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    assert (await TaskService(db_session).get_task(task.id)).output_data[
        "text"
    ] == "Тестовый результат агента"
    assert (await TaskService(db_session).get_task(downstream.id)).status is TaskStatus.READY


async def test_failure_retry_and_cancellation_race(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    first = await service.create_queued_run(task.id)
    assert await service.claim(first.id)
    await service.finish_failure(first.id, AgentRuntimeError("AGENT_PROVIDER_ERROR", "Safe error"))
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.READY
    second = await service.create_queued_run(task.id, retry=True)
    assert second.id != first.id
    assert (await TaskService(db_session).get_task(task.id)).retry_count == 1
    assert await service.claim(second.id)
    await TaskService(db_session).cancel_task(task.id)
    await service.finish_success(second.id, RuntimeResult({"text": "late"}, 1, 1, 1, 2, None))
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.CANCELLED
    assert (await TaskService(db_session).get_task(task.id)).output_data == {}
    assert (await service.get_run(second.id)).status is AgentRunStatus.CANCELLED


async def test_inactive_missing_model_and_retry_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", None)
    _, agent, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    with pytest.raises(AppError) as model_error:
        await service.create_queued_run(task.id)
    assert model_error.value.code == "AGENT_MODEL_NOT_CONFIGURED"
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    agent.status = AgentStatus.INACTIVE
    await db_session.commit()
    with pytest.raises(AppError) as inactive:
        await service.create_queued_run(task.id)
    assert inactive.value.code == "AGENT_INACTIVE"
    agent.status = AgentStatus.ACTIVE
    task.status = TaskStatus.FAILED
    task.retry_count = settings.agent_max_retries
    await db_session.commit()
    with pytest.raises(AppError) as limit:
        await service.create_queued_run(task.id, retry=True)
    assert limit.value.code == "TASK_RETRY_LIMIT_REACHED"


async def test_runner_normalizes_usage_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    usage = SimpleNamespace(requests=2, input_tokens=11, output_tokens=7, total_tokens=18)
    fake = SimpleNamespace(final_output="Result", context_wrapper=SimpleNamespace(usage=usage))

    async def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return fake

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", fake_run)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4())
    result = await AgentRunnerService().run(
        AgentSnapshot("Agent", "Prompt", "model", []), "Input", context, None
    )
    assert result.output_data == {"text": "Result"} and result.total_tokens == 18

    async def failure(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret provider detail")

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", failure)
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("Agent", "Prompt", "model", []), "Input", context, None
        )
    assert error.value.code == "AGENT_PROVIDER_ERROR" and "secret" not in str(error.value)


async def test_agent_run_api_and_retry(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    owner, _, _, task = await runtime_fixture(db_session)

    class QueueResult:
        id = "queue-job-id"

    monkeypatch.setattr(
        "app.workers.agent_worker.execute_agent_run.delay",
        lambda _run_id: QueueResult(),
    )
    assert (await client.get("/api/v1/agent-runs")).status_code == 401
    login = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": "password"}
    )
    assert login.status_code == 200
    response = await client.post(f"/api/v1/tasks/{task.id}/run")
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert response.json()["status"] == "QUEUED"
    assert (await client.post(f"/api/v1/tasks/{task.id}/run")).status_code == 409
    history = await client.get(f"/api/v1/agent-runs?task_id={task.id}")
    assert history.status_code == 200 and len(history.json()) == 1
    details = await client.get(f"/api/v1/agent-runs/{run_id}")
    assert details.status_code == 200
    assert details.json()["prompt_snapshot"] == "Runtime prompt"
    service = AgentRunService(db_session)
    assert await service.claim(UUID(run_id))
    await service.finish_failure(
        UUID(run_id), AgentRuntimeError("AGENT_PROVIDER_ERROR", "Safe error")
    )
    retry = await client.post(f"/api/v1/tasks/{task.id}/retry")
    assert retry.status_code == 202 and retry.json()["id"] != run_id


async def test_runner_normalizes_structured_campaign_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    usage = SimpleNamespace(requests=1, input_tokens=5, output_tokens=6, total_tokens=11)
    plan = CampaignPlan.model_validate(plan_data())
    fake = SimpleNamespace(final_output=plan, context_wrapper=SimpleNamespace(usage=usage))

    async def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return fake

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", fake_run)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CAMPAIGN_PLANNING)
    result = await AgentRunnerService().run(
        AgentSnapshot("Director", "Prompt", "model", [], CampaignPlan),
        "Input",
        context,
        None,
    )
    assert result.output_data["main_message"] == plan.main_message
