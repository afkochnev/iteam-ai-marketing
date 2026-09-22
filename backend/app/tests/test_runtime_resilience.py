import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents.exceptions import MaxTurnsExceeded

from app.agents.factory import AgentRuntimeContext, AgentSnapshot
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent_run import AgentRunStatus
from app.models.task import TaskStatus, TaskType
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import AgentRunnerService, AgentRuntimeError, RuntimeResult
from app.services.retry_policy import can_retry, classify_error, retry_exhausted
from app.services.task_recovery_service import TaskRecoveryService
from app.services.task_service import TaskService
from app.tests.test_agent_runtime import runtime_fixture


def test_retry_classifier_distinguishes_transient_and_business_errors() -> None:
    assert can_retry("AGENT_PROVIDER_ERROR", 0)
    assert can_retry("AGENT_TIMEOUT", settings.agent_max_retries - 1)
    assert not can_retry("INSUFFICIENT_KNOWLEDGE", 0)
    assert not can_retry("INVALID_SOCIAL_SOURCE", 0)
    assert retry_exhausted("AGENT_TIMEOUT", settings.agent_max_retries)
    assert classify_error("INSUFFICIENT_KNOWLEDGE").retryable is False
    assert classify_error("AGENT_TIMEOUT", 0).delay_seconds == settings.agent_retry_backoff_seconds
    assert (
        classify_error("AGENT_TIMEOUT", 1).delay_seconds == settings.agent_retry_backoff_seconds * 2
    )
    assert (
        classify_error("AGENT_TIMEOUT", 2).delay_seconds == settings.agent_retry_backoff_seconds * 4
    )


@pytest.mark.asyncio
async def test_transient_failure_returns_task_to_ready_then_exhausts(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await service.finish_failure(run.id, AgentRuntimeError("AGENT_TIMEOUT", "timed out"))
    await db_session.refresh(task)
    assert task.status is TaskStatus.READY
    assert task.retry_count == 1

    second = await service.create_queued_run(task.id)
    assert await service.claim(second.id)
    task.retry_count = settings.agent_max_retries
    await db_session.commit()
    await service.finish_failure(second.id, AgentRuntimeError("AGENT_TIMEOUT", "timed out"))
    await db_session.refresh(task)
    assert task.status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_stuck_run_recovery_marks_run_and_task_failed(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    run.started_at = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds + 10)
    await db_session.commit()

    recovered = await TaskRecoveryService(db_session).recover_stuck()
    assert [item.id for item in recovered] == [run.id]
    await db_session.refresh(run)
    await db_session.refresh(task)
    assert run.status is AgentRunStatus.FAILED
    assert run.error_code == "AGENT_STUCK"
    assert task.status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_late_completion_of_failed_run_is_ignored(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await service.finish_failure(run.id, AgentRuntimeError("INSUFFICIENT_KNOWLEDGE", "not enough"))
    await service.finish_success(run.id, RuntimeResult({"unexpected": True}, 1, 1, 1, 2, None))
    await db_session.refresh(task)
    assert task.output_data == {}
    assert task.status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_cancelled_task_rejects_late_failure(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    task.status = TaskStatus.CANCELLED
    await db_session.commit()
    await service.finish_failure(run.id, AgentRuntimeError("AGENT_TIMEOUT", "late"))
    await db_session.refresh(run)
    assert run.status is AgentRunStatus.CANCELLED


@pytest.mark.asyncio
async def test_ready_and_blocked_tasks_can_be_cancelled_without_a_run(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, campaign, ready = await runtime_fixture(db_session)
    blocked = await TaskService(db_session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            title="Blocked",
            task_type=ready.task_type,
            assigned_agent_id=ready.assigned_agent_id,
        )
    )
    blocked.status = TaskStatus.BLOCKED
    await db_session.commit()
    await TaskService(db_session).cancel_task(ready.id)
    await TaskService(db_session).cancel_task(blocked.id)
    await db_session.refresh(ready)
    await db_session.refresh(blocked)
    assert ready.status is TaskStatus.CANCELLED
    assert blocked.status is TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_runner_timeout_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_run(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0.02)

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_run_timeout_seconds", 0.001)
    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", slow_run)
    with pytest.raises(AgentRuntimeError, match="Превышено время") as error:
        await AgentRunnerService().run(
            AgentSnapshot("Agent", "Prompt", "model", []),
            "Input",
            AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4()),
            None,
        )
    assert error.value.code == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_runner_max_turns_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def too_many_turns(*args: object, **kwargs: object) -> None:
        raise MaxTurnsExceeded("limit")

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", too_many_turns)
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("Agent", "Prompt", "model", []),
            "Input",
            AgentRuntimeContext(uuid4(), uuid4(), uuid4(), uuid4()),
            None,
        )
    assert error.value.code == "AGENT_MAX_TURNS_EXCEEDED"


@pytest.mark.asyncio
async def test_structured_output_repair_exhaustion_fails_without_retry(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    attempts = 0
    usage = SimpleNamespace(requests=1, input_tokens=1, output_tokens=1, total_tokens=2)

    async def invalid_run(*args: object, **kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        return SimpleNamespace(
            final_output={"invalid": True}, context_wrapper=SimpleNamespace(usage=usage)
        )

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", invalid_run)
    monkeypatch.setattr(settings, "agent_output_repair_attempts", 2)
    context = AgentRuntimeContext(
        uuid4(), uuid4(), uuid4(), uuid4(), task_type=TaskType.WRITE_ARTICLE
    )
    with pytest.raises(AgentRuntimeError) as error:
        await AgentRunnerService().run(
            AgentSnapshot("Writer", "Prompt", "model", []), "Input", context, None
        )
    assert attempts == 3
    assert error.value.code == "INVALID_ARTICLE_RESULT"
    assert not can_retry(error.value.code, 0)

    _, _, _, task = await runtime_fixture(db_session)
    run_service = AgentRunService(db_session)
    run = await run_service.create_queued_run(task.id)
    assert await run_service.claim(run.id)
    await run_service.finish_failure(run.id, error.value)
    await db_session.refresh(run)
    await db_session.refresh(task)
    assert run.status is AgentRunStatus.FAILED
    assert task.status is TaskStatus.FAILED
    assert run.completed_at is not None
    assert task.retry_count == 0


@pytest.mark.asyncio
async def test_timeout_retry_run_b_rejects_late_run_a_result(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)

    run_a = await service.create_queued_run(task.id)
    assert await service.claim(run_a.id)
    await service.finish_failure(
        run_a.id,
        AgentRuntimeError("AGENT_TIMEOUT", "Agent execution timed out."),
    )
    await db_session.refresh(task)
    assert run_a.status is AgentRunStatus.FAILED
    assert task.status is TaskStatus.READY
    assert task.retry_count == 1

    run_b = await service.create_queued_run(task.id, retry=True)
    assert await service.claim(run_b.id)
    await db_session.refresh(task)
    assert task.status is TaskStatus.IN_PROGRESS

    late = RuntimeResult(
        output_data={"text": "stale result"},
        request_count=1,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        trace_id="late",
    )
    await service.finish_success(run_a.id, late)
    await db_session.refresh(run_a)
    await db_session.refresh(run_b)
    await db_session.refresh(task)
    assert run_a.status is AgentRunStatus.FAILED
    assert run_b.status is AgentRunStatus.RUNNING
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.output_data == {}

    await service.finish_success(
        run_b.id,
        RuntimeResult(
            output_data={"text": "authoritative result"},
            request_count=1,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            trace_id="authoritative",
        ),
    )
    await db_session.refresh(run_a)
    await db_session.refresh(run_b)
    await db_session.refresh(task)
    assert run_a.status is AgentRunStatus.FAILED
    assert run_b.status is AgentRunStatus.COMPLETED
    assert task.status is TaskStatus.COMPLETED
    assert task.output_data == {"text": "authoritative result"}


@pytest.mark.asyncio
async def test_two_recovery_sessions_recover_one_stale_run(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    run.started_at = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds + 10)
    await db_session.commit()

    async def recover() -> int:
        async with async_session_factory() as session:
            return len(await TaskRecoveryService(session).recover_stuck())

    first, second = await asyncio.gather(recover(), recover())
    assert first + second == 1
    await db_session.refresh(run)
    await db_session.refresh(task)
    assert run.status is AgentRunStatus.FAILED
    assert task.status is TaskStatus.FAILED
