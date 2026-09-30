import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from httpx import AsyncClient, Request
from openai import APITimeoutError
from pydantic import ValidationError
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
from app.schemas.agent_outputs import CampaignPlan, SingleSocialPostResult, SocialPostPackResult
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


def test_agents_sdk_context_wrapper_compatibility() -> None:
    """The installed SDK must construct its default usage without network I/O."""

    from agents.run_context import RunContextWrapper

    wrapper = RunContextWrapper(context=None)
    assert wrapper.usage.input_tokens_details.cached_tokens == 0


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


async def test_runner_normalizes_usage_and_errors(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
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
        raise RuntimeError(
            "provider detail OPENAI_API_KEY=sk-live-secret Authorization: Bearer bearer-secret"
        )

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", failure)
    caplog.set_level(logging.ERROR, logger="app.services.agent_runner_service")
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("Agent", "Prompt", "model", []), "Input", context, None
        )
    assert error.value.code == "AGENT_PROVIDER_ERROR" and "secret" not in str(error.value)
    provider_record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "agent_provider_error"
    )
    assert provider_record.exception_type == "RuntimeError"
    assert provider_record.agent_id == str(context.agent_id)
    assert provider_record.task_id == str(context.task_id)
    assert provider_record.agent_run_id == str(context.agent_run_id)
    assert provider_record.model == "model"
    assert "RuntimeError" in caplog.text
    assert "provider detail" in caplog.text
    assert "sk-live-secret" not in caplog.text
    assert "bearer-secret" not in caplog.text


async def test_runner_preserves_claimed_single_social_post_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    usage = SimpleNamespace(requests=1, input_tokens=2, output_tokens=3, total_tokens=5)
    post = {
        "key": "diagnostic",
        "channel": "TELEGRAM",
        "title": "Диагностика",
        "text_markdown": "Команда может спорить о выполнении курса или о самом направлении.",
        "cta": "",
        "sources": [{"content_version_id": str(uuid4()), "section_key": "signal_1"}],
        "suggested_publish_order": 1,
    }

    async def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            final_output={
                "sufficient": True,
                "pack": {"strategy_summary": "Диагностика", "posts": [post]},
            },
            context_wrapper=SimpleNamespace(usage=usage),
        )

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", fake_run)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CREATE_SOCIAL_POSTS)
    result = await AgentRunnerService().run(
        AgentSnapshot("SMM", "Prompt", "model", [], SingleSocialPostResult),
        "Generate one plan-item post",
        context,
        None,
    )

    assert len(result.output_data["pack"]["posts"]) == 1  # type: ignore[index]


async def test_campaign_plan_sdk_schema_error_enters_bounded_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    calls = 0
    usage = SimpleNamespace(requests=1, input_tokens=1, output_tokens=1, total_tokens=2)

    async def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            invalid = plan_data()
            invalid["tasks"][0]["agent_slug"] = "writer"
            try:
                CampaignPlan.model_validate(invalid)
            except ValidationError as validation_error:
                raise ModelBehaviorError(
                    "Invalid JSON when parsing CampaignPlan tasks.9/tasks.10"
                ) from validation_error
        return SimpleNamespace(
            final_output=plan_data(), context_wrapper=SimpleNamespace(usage=usage)
        )

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", fake_run)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CAMPAIGN_PLANNING)
    result = await AgentRunnerService().run(
        AgentSnapshot("Marketing Director", "Prompt", "model", [], CampaignPlan),
        "Create a campaign plan",
        context,
        None,
    )

    assert calls == 2
    assert result.output_data["tasks"][0]["agent_slug"] == "knowledge_keeper"


async def test_campaign_plan_schema_repair_exhaustion_is_invalid_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_output_repair_attempts", 2)
    calls = 0

    async def always_invalid(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        try:
            CampaignPlan.model_validate({"invalid": True})
        except ValidationError as validation_error:
            raise ModelBehaviorError("Invalid JSON when parsing CampaignPlan") from validation_error
        raise AssertionError("unreachable")

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", always_invalid)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CAMPAIGN_PLANNING)
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("Marketing Director", "Prompt", "model", [], CampaignPlan),
            "Create a campaign plan",
            context,
            None,
        )

    assert calls == 3
    assert error.value.code == "INVALID_CAMPAIGN_PLAN"


async def test_non_structured_model_behavior_error_remains_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")

    async def genuine_failure(*args: object, **kwargs: object) -> SimpleNamespace:
        raise ModelBehaviorError("Model produced an unsupported tool call")

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", genuine_failure)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CAMPAIGN_PLANNING)
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("Marketing Director", "Prompt", "model", [], CampaignPlan),
            "Create a campaign plan",
            context,
            None,
        )
    assert error.value.code == "AGENT_MODEL_BEHAVIOR_ERROR"


async def test_successful_agent_run_clears_stale_task_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _owner, _agent, _campaign, task = await runtime_fixture(db_session)
    task.error_message = "old provider failure"
    await db_session.commit()
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await service.finish_success(run.id, RuntimeResult({"text": "ok"}, 1, 1, 1, 2, None))
    refreshed = await service.get_run(run.id)
    assert refreshed.status is AgentRunStatus.COMPLETED
    assert (await TaskService(db_session).get_task(task.id)).error_message is None


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


async def test_smm_runtime_uses_bounded_turns_timeout_and_only_article_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_run_timeout_seconds", 180)
    monkeypatch.setattr(settings, "agent_provider_request_timeout_seconds", 60)
    monkeypatch.setattr(settings, "agent_provider_max_retries", 0)
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def close(self) -> None:
            return None

    usage = SimpleNamespace(requests=2, input_tokens=1, output_tokens=1, total_tokens=2)
    output = SocialPostPackResult.model_validate(
        {"sufficient": False, "pack": None, "gaps": ["Недостаточно данных"]}
    )

    async def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        captured["max_turns"] = kwargs["max_turns"]
        return SimpleNamespace(final_output=output, context_wrapper=SimpleNamespace(usage=usage))

    resolved: list[list[str]] = []

    def resolve_tools(names: list[str]) -> list[object]:
        resolved.append(list(names))
        return []

    monkeypatch.setattr("app.services.agent_runner_service.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", fake_run)
    monkeypatch.setattr("app.services.agent_runner_service.tool_registry.resolve", resolve_tools)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CREATE_SOCIAL_POSTS)
    result = await AgentRunnerService().run(
        AgentSnapshot(
            "SMM",
            "Prompt",
            "model",
            ["read_content_version", "search_knowledge"],
            SocialPostPackResult,
        ),
        "Generate posts",
        context,
        None,
    )

    assert result.output_data["sufficient"] is False
    assert captured["max_turns"] == settings.smm_agent_max_turns
    assert captured["timeout"] == settings.smm_final_provider_timeout_seconds
    assert captured["max_retries"] == 0
    assert resolved == [["read_content_version"]]


async def test_smm_turn_exhaustion_is_explicit_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    calls = 0

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            return None

        async def close(self) -> None:
            return None

    async def exhausted(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        assert kwargs["max_turns"] == settings.smm_agent_max_turns
        raise MaxTurnsExceeded("turn limit")

    monkeypatch.setattr("app.services.agent_runner_service.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", exhausted)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CREATE_SOCIAL_POSTS)
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("SMM", "Prompt", "model", [], SocialPostPackResult),
            "Generate posts",
            context,
            None,
        )

    assert calls == 1
    assert error.value.code == "SMM_MAX_TURNS_EXCEEDED"


async def test_smm_provider_timeout_is_distinct_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_run_timeout_seconds", 2)
    monkeypatch.setattr(settings, "agent_provider_request_timeout_seconds", 60)
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def close(self) -> None:
            captured["closed"] = True

    async def hung_provider(*args: object, **kwargs: object) -> None:
        raise APITimeoutError(request=Request("POST", "https://api.openai.com/v1/responses"))

    monkeypatch.setattr("app.services.agent_runner_service.AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", hung_provider)
    context = AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4(), TaskType.CREATE_SOCIAL_POSTS)
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("SMM", "Prompt", "model", [], SocialPostPackResult),
            "Generate posts",
            context,
            None,
        )

    assert error.value.code == "AGENT_PROVIDER_TIMEOUT"
    assert captured["timeout"] < settings.agent_run_timeout_seconds
    assert captured["max_retries"] == 0
    assert captured["closed"] is True


async def test_smm_phase_timeout_is_attached_to_sdk_request_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout must reach the SDK's actual responses.create kwargs."""

    from agents import ModelSettings

    from app.services.agent_runner_service import _InstrumentedResponsesModel

    requests: list[dict[str, object]] = []

    class FakeResponses:
        async def create(self, **kwargs: object) -> SimpleNamespace:
            requests.append(kwargs)
            return SimpleNamespace(output=[], usage=None, id="response-test")

    class FakeClient:
        responses = FakeResponses()

    tracing = SimpleNamespace(is_disabled=lambda: True, include_data=lambda: False)
    loop = __import__("asyncio").get_running_loop()
    model = _InstrumentedResponsesModel(
        "gpt-test",
        FakeClient(),
        agent_run_id=str(uuid4()),
        task_id=str(uuid4()),
        normal_timeout=60,
        final_timeout=120,
        deadline=loop.time() + 180,
        repair=False,
    )
    settings_arg = ModelSettings()
    await model.get_response("system", "input", settings_arg, [], None, [], tracing)
    await model.get_response("system", "input", settings_arg, [], None, [], tracing)

    assert requests[0]["timeout"] == pytest.approx(60, abs=0.2)
    assert requests[1]["timeout"] == pytest.approx(120, abs=0.2)

    short_model = _InstrumentedResponsesModel(
        "gpt-test",
        FakeClient(),
        agent_run_id=str(uuid4()),
        task_id=str(uuid4()),
        normal_timeout=60,
        final_timeout=120,
        deadline=loop.time() + 35,
        repair=False,
    )
    short_settings = ModelSettings()
    await short_model.get_response("system", "input", short_settings, [], None, [], tracing)
    assert requests[-1]["timeout"] == pytest.approx(34.9, abs=0.3)


def test_smm_repair_contains_deterministic_pack_shape_diagnostics() -> None:
    from app.services.agent_runner_service import _repair_input

    invalid = {
        "sufficient": True,
        "pack": {
            "strategy_summary": "series",
            "posts": [
                {
                    "key": f"post-{index}",
                    "channel": "TELEGRAM" if index % 2 else "VK",
                    "title": f"Post {index}",
                    "text_markdown": "text",
                    "cta": "cta",
                    "sources": [{"content_version_id": str(uuid4()), "section_key": "choice"}],
                    "suggested_publish_order": ((index - 1) % 3) + 1,
                }
                for index in range(1, 7)
            ],
        },
    }
    try:
        SocialPostPackResult.model_validate(invalid)
    except ValidationError as validation_error:
        error = ModelBehaviorError("Invalid JSON when parsing SocialPostPackResult")
        error.__cause__ = validation_error
    else:  # pragma: no cover - the fixture is intentionally invalid
        raise AssertionError("expected invalid SocialPostPackResult")

    guidance = _repair_input(
        "social_strategy={'channels': ['TELEGRAM', 'VK'], 'post_count': 9}",
        error,
        TaskType.CREATE_SOCIAL_POSTS,
    )
    assert "expected_post_count=9" in guidance
    assert "actual_post_count=6" in guidance
    assert "actual_publish_orders=[1, 2, 3, 1, 2, 3]" in guidance
    assert "duplicate_publish_orders=[1, 2, 3]" in guidance
    assert "missing_publish_orders=[4, 5, 6, 7, 8, 9]" in guidance
    assert "глобальный для всего пакета" in guidance
    assert "plain text" in guidance
    assert "внутренних меток" in guidance
    assert "опытный консультант iTeam" in guidance
    assert "Часто вижу" in guidance
    assert "чередуй наблюдение" in guidance


async def test_smm_repair_reuses_cached_article_without_exposing_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    resolved: list[list[str]] = []
    inputs: list[str] = []
    usage = SimpleNamespace(requests=1, input_tokens=1, output_tokens=1, total_tokens=2)
    valid = {
        "sufficient": True,
        "pack": {
            "strategy_summary": "series",
            "posts": [
                {
                    "key": f"post-{index}",
                    "channel": "TELEGRAM" if index % 2 else "VK",
                    "title": f"Post {index}",
                    "text_markdown": "text",
                    "cta": "cta",
                    "sources": [{"content_version_id": str(uuid4()), "section_key": "choice"}],
                    "suggested_publish_order": index,
                }
                for index in range(1, 6)
            ],
        },
    }
    invalid = dict(valid)
    invalid["pack"] = dict(valid["pack"])
    invalid["pack"]["posts"] = list(valid["pack"]["posts"])
    invalid["pack"]["posts"][1] = dict(invalid["pack"]["posts"][1])
    invalid["pack"]["posts"][1]["suggested_publish_order"] = 1
    invalid["pack"]["posts"][0] = dict(invalid["pack"]["posts"][0])
    invalid["pack"]["posts"][0]["text_markdown"] = "**служебный черновик**"
    calls = 0

    def resolve(names: list[str]) -> list[object]:
        resolved.append(list(names))
        return []

    async def fake_run(_agent: object, text: str, **_: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        inputs.append(text)
        if calls == 1:
            try:
                SocialPostPackResult.model_validate(invalid)
            except ValidationError as validation_error:
                raise ModelBehaviorError(
                    "Invalid JSON when parsing SocialPostPackResult"
                ) from validation_error
        return SimpleNamespace(
            final_output=SocialPostPackResult.model_validate(valid),
            context_wrapper=SimpleNamespace(usage=usage),
        )

    monkeypatch.setattr("app.services.agent_runner_service.tool_registry.resolve", resolve)
    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", fake_run)
    context = AgentRuntimeContext(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        TaskType.CREATE_SOCIAL_POSTS,
        allowed_content_version_ids=(uuid4(),),
        content_version_cache={"cached": '{"title":"Article","article":{}}'},
    )
    result = await AgentRunnerService().run(
        AgentSnapshot("SMM", "Prompt", "model", ["read_content_version"], SocialPostPackResult),
        "social_strategy={'channels': ['TELEGRAM', 'VK'], 'post_count': 5}",
        context,
        None,
    )

    assert result.output_data["sufficient"] is True
    assert calls == 2
    assert resolved == [["read_content_version"], []]
    assert "Article" in inputs[1]
    assert "**" not in result.output_data["pack"]["posts"][0]["text_markdown"]
