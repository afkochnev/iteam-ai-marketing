import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.agents.factory import create_runtime_agent
from app.api.dependencies import get_current_user
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.director_chat import CHAT_PROMPT
from app.core.errors import AppError
from app.main import app
from app.models.agent import AgentStatus, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import CampaignStatus
from app.models.marketing_chat import (
    MarketingContextSnapshot,
    MarketingConversation,
    MarketingMessage,
)
from app.models.marketing_chat import (
    MarketingMessageRole as Role,
)
from app.models.marketing_chat import (
    MarketingMessageStatus as Status,
)
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.tasks import TaskRepository
from app.schemas.marketing_chat import DirectorChatReply, MessageSend
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import AgentRuntimeError, RuntimeResult
from app.services.campaign_workspace_service import CampaignWorkspaceService
from app.services.director_chat_service import (
    DirectorChatService,
    build_context,
    build_snapshot,
    snapshot_hash,
)
from app.services.task_recovery_service import TaskRecoveryService
from app.tests.test_agent_runtime import runtime_fixture
from app.workers.agent_worker import execute_agent_run


@pytest.fixture
async def chat(db_session, monkeypatch):
    owner, agent, campaign, task = await runtime_fixture(db_session)
    owner.role = UserRole.MANAGER
    agent.slug = "marketing_director"
    agent.settings = {"chat_prompt": CHAT_PROMPT}
    agent.model = "mock-director"
    db_session.add(AgentTool(agent_id=agent.id, tool_name="create_internal_task", is_enabled=True))
    await db_session.commit()
    calls = []

    def enqueue(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=str(uuid4()))

    monkeypatch.setattr(execute_agent_run, "apply_async", enqueue)
    service = DirectorChatService(db_session)
    conversation = await service.create(campaign.id, owner, "Campaign discussion")
    return SimpleNamespace(
        owner=owner,
        agent=agent,
        campaign=campaign,
        task=task,
        conversation=conversation,
        service=service,
        calls=calls,
    )


async def turn(chat):
    return await chat.service.send(
        chat.conversation.id, chat.owner, "Какой следующий шаг?", uuid4()
    )


async def count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


async def test_create_persistent_conversation(db_session, chat):
    async with async_session_factory() as other:
        value = await other.get(MarketingConversation, chat.conversation.id)
        assert value.campaign_id == chat.campaign.id
        assert value.created_by_user_id == chat.owner.id
    assert len(await chat.service.list_conversations(chat.campaign.id, chat.owner)) == 1


async def test_other_manager_denied_and_admin_allowed(chat):
    stranger = User(id=uuid4(), role=UserRole.MANAGER)
    with pytest.raises(AppError) as error:
        await chat.service.conversation(chat.conversation.id, stranger)
    assert error.value.status_code == 404
    assert await chat.service.list_conversations(chat.campaign.id, stranger) == []
    admin = User(id=uuid4(), role=UserRole.ADMIN)
    assert (await chat.service.conversation(chat.conversation.id, admin)).id == chat.conversation.id


async def test_archived_conversation_read_only(chat):
    await chat.service.archive(chat.conversation.id, chat.owner)
    assert await chat.service.messages(chat.conversation.id, chat.owner) == []
    with pytest.raises(AppError):
        await turn(chat)
    assert not chat.calls


async def test_archived_campaign_history_readable_no_create_send(db_session, chat):
    chat.campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    assert await chat.service.messages(chat.conversation.id, chat.owner) == []
    for action in [
        lambda: turn(chat),
        lambda: chat.service.create(chat.campaign.id, chat.owner, None),
    ]:
        with pytest.raises(AppError):
            await action()
    assert not chat.calls


async def test_persisted_before_enqueue_and_ai_queue(db_session, chat, monkeypatch):
    original = chat.service._enqueue

    async def verify(run):
        async with async_session_factory() as independent:
            assert await count(independent, MarketingMessage) == 2
            assert await count(independent, MarketingContextSnapshot) == 1
            assert await independent.get(AgentRun, run.id)
        await original(run)

    monkeypatch.setattr(chat.service, "_enqueue", verify)
    result = await turn(chat)
    assert result.assistant_message.status is Status.PENDING
    assert chat.calls[0]["queue"] == "ai"
    run = await db_session.get(AgentRun, result.assistant_message.agent_run_id)
    assert chat.calls[0]["args"] == [str(run.id)]
    assert run.prompt_snapshot == CHAT_PROMPT
    assert run.prompt_hash == snapshot_hash_prompt(CHAT_PROMPT)
    assert run.status is AgentRunStatus.QUEUED


def snapshot_hash_prompt(prompt):
    import hashlib

    return hashlib.sha256(prompt.encode()).hexdigest()


async def test_snapshot_hash_frozen_and_database_immutable(db_session, chat):
    result = await turn(chat)
    frozen = await db_session.get(MarketingContextSnapshot, result.user_message.context_snapshot_id)
    original = frozen.snapshot_hash
    assert original == snapshot_hash(frozen.snapshot)
    chat.campaign.goal = "Changed after enqueue"
    await db_session.commit()
    await db_session.refresh(frozen)
    assert frozen.snapshot["campaign"]["goal"] == "Test runtime"
    assert frozen.snapshot_hash == original
    with pytest.raises(DBAPIError):
        await db_session.execute(
            update(MarketingContextSnapshot)
            .where(MarketingContextSnapshot.id == frozen.id)
            .values(strategy_version=999)
        )
    await db_session.rollback()


async def test_exact_approved_strategy_and_reference_allowlist(db_session, chat):
    db_session.add_all(
        [
            Approval(
                object_type=ApprovalObjectType.CAMPAIGN_STRATEGY,
                object_id=chat.campaign.id,
                subject_version=1,
                status=ApprovalStatus.APPROVED,
                subject_snapshot={"approved": "exact"},
            ),
            Approval(
                object_type=ApprovalObjectType.CAMPAIGN_STRATEGY,
                object_id=chat.campaign.id,
                subject_version=2,
                status=ApprovalStatus.PENDING,
                subject_snapshot={"pending": "proposal"},
            ),
        ]
    )
    chat.campaign.strategy_version = 2
    chat.campaign.strategy = {"pending": "proposal"}
    await db_session.commit()
    facts = await build_snapshot(db_session, chat.campaign.id)
    assert facts["strategy"]["approved_snapshot"] == {"approved": "exact"}
    assert facts["strategy"]["approved_version"] == 1
    assert "strategy" not in facts["campaign"]
    assert {"entity_type": "campaign", "entity_id": str(chat.campaign.id)} in facts[
        "reference_allowlist"
    ]


async def test_duplicate_client_id_one_turn(db_session, chat):
    key = uuid4()
    first = await chat.service.send(chat.conversation.id, chat.owner, "First", key)
    second = await chat.service.send(chat.conversation.id, chat.owner, "Repeated", key)
    assert first == second
    assert await count(db_session, MarketingMessage) == 2
    assert await count(db_session, AgentRun) == 1
    assert len(chat.calls) == 1


async def test_second_message_conflict(chat):
    await turn(chat)
    with pytest.raises(AppError) as error:
        await turn(chat)
    assert error.value.code == "DIRECTOR_CHAT_TURN_IN_PROGRESS"


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_turn_serialized(db_session, chat, same_key):
    conversation_id, owner_id = chat.conversation.id, chat.owner.id
    key = uuid4()
    # Release the fixture session's lock before independent transactions.
    await db_session.commit()

    async def send(client):
        async with async_session_factory() as session:
            user = await session.get(User, owner_id)
            try:
                return await DirectorChatService(session).send(
                    conversation_id, user, "Concurrent", client
                )
            except AppError as error:
                return error.code

    results = await asyncio.gather(send(key), send(key if same_key else uuid4()))
    if same_key:
        assert results[0] == results[1]
    else:
        assert "DIRECTOR_CHAT_TURN_IN_PROGRESS" in results
    assert await count(db_session, MarketingMessage) == 2
    assert await count(db_session, AgentRun) == 1


async def test_internal_manual_hidden_from_workspace_and_tasks(db_session, chat):
    before = (await CampaignWorkspaceService(db_session).get(chat.campaign.id)).model_dump()
    result = await turn(chat)
    task = await db_session.get(Task, result.assistant_message.task_id)
    assert task.is_internal and task.task_type is TaskType.MANUAL
    assert task.input_data["internal_kind"] == "MARKETING_DIRECTOR_CHAT"
    assert task.id not in [item.id for item in await TaskRepository(db_session).list_tasks()]
    after = (await CampaignWorkspaceService(db_session).get(chat.campaign.id)).model_dump()
    assert before == after


@pytest.mark.parametrize(
    "broken,code",
    [
        ("inactive", "AGENT_INACTIVE"),
        ("prompt", "DIRECTOR_CHAT_PROMPT_NOT_CONFIGURED"),
        ("model", "AGENT_MODEL_NOT_CONFIGURED"),
    ],
)
async def test_misconfigured_director_safe_before_enqueue(
    db_session, chat, monkeypatch, broken, code
):
    if broken == "inactive":
        chat.agent.status = AgentStatus.INACTIVE
    elif broken == "prompt":
        chat.agent.settings = {}
    else:
        chat.agent.model = None
        monkeypatch.setattr(settings, "openai_default_model", None)
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await turn(chat)
    assert error.value.code == code
    assert await count(db_session, MarketingMessage) == 0
    assert not chat.calls


async def test_claim_zero_executable_tools(db_session, chat):
    result = await turn(chat)
    claimed = await AgentRunService(db_session).claim(result.assistant_message.agent_run_id)
    snapshot, text, context, _ = claimed
    assert snapshot.enabled_tool_names == []
    assert create_runtime_agent(snapshot, []).tools == []
    assert snapshot.output_type is DirectorChatReply
    assert "SYSTEM SNAPSHOT" in text and "CURRENT USER MESSAGE" in text
    assert context.task_type is TaskType.MANUAL
    assert chat.agent.system_prompt == "Runtime prompt"


@pytest.mark.parametrize(
    "data",
    [
        {"message": " "},
        {"message": "x", "href": "https://evil.test"},
        {
            "message": "x",
            "references": [{"entity_type": "unknown", "entity_id": str(uuid4()), "label": "x"}],
        },
        {
            "message": "x",
            "references": [
                {
                    "entity_type": "campaign",
                    "entity_id": str(uuid4()),
                    "label": "x",
                    "href": "/evil",
                }
            ],
        },
        {"message": "x" * 6001},
        {"message": "x", "limitations": ["x"] * 9},
    ],
)
def test_strict_output_validation(data):
    with pytest.raises(ValidationError):
        DirectorChatReply.model_validate(data)


@pytest.mark.parametrize("text", ["", "  ", "\n\t"])
def test_empty_send_rejected(text):
    with pytest.raises(ValidationError):
        MessageSend(content=text, client_message_id=uuid4())


async def test_success_persists_verified_refs_drops_inventions(db_session, chat):
    result = await turn(chat)
    runtime = AgentRunService(db_session)
    await runtime.claim(result.assistant_message.agent_run_id)
    payload = {
        "message": "Сначала утвердите стратегию.",
        "references": [
            {"entity_type": "campaign", "entity_id": str(chat.campaign.id), "label": "Кампания"},
            {"entity_type": "task", "entity_id": str(uuid4()), "label": "Invented"},
        ],
        "limitations": ["Нет утверждённой стратегии"],
    }
    await runtime.finish_success(
        result.assistant_message.agent_run_id, RuntimeResult(payload, 1, 2, 3, 5, None)
    )
    messages = await chat.service.messages(chat.conversation.id, chat.owner)
    assistant = messages[-1]
    assert assistant.status is Status.COMPLETED and assistant.content == payload["message"]
    assert len(assistant.references) == 1
    assert assistant.references[0]["href"] == f"/campaigns/{chat.campaign.id}"
    assert assistant.limitations == payload["limitations"]
    run = await db_session.get(AgentRun, assistant.agent_run_id)
    assert run.output_data["reference_validation"]["discarded_count"] == 1
    task = await db_session.get(Task, assistant.task_id)
    assert task.status is TaskStatus.COMPLETED
    assert chat.campaign.status is CampaignStatus.DRAFT


async def test_provider_failure_retry_same_snapshot_no_duplicates(db_session, chat):
    result = await turn(chat)
    runtime = AgentRunService(db_session)
    old_id = result.assistant_message.agent_run_id
    before = (await db_session.get(AgentRun, old_id)).input_data["text"]
    await runtime.claim(old_id)
    await runtime.finish_failure(
        old_id, AgentRuntimeError("AGENT_PROVIDER_ERROR", "secret provider detail")
    )
    assistant = (await chat.service.messages(chat.conversation.id, chat.owner))[-1]
    assert assistant.status is Status.FAILED and assistant.content == ""
    assert "secret" not in assistant.error_message
    task = await db_session.get(Task, assistant.task_id)
    assert task.status is TaskStatus.FAILED and task.retry_count == 0
    chat.campaign.goal = "Changed"
    chat.agent.settings = {"chat_prompt": "Changed prompt"}
    chat.agent.model = "Changed model"
    await db_session.commit()
    retried = await chat.service.retry(chat.conversation.id, assistant.id, chat.owner)
    new_id = retried.agent_run_id
    assert new_id != old_id
    new_run = await db_session.get(AgentRun, new_id)
    assert new_run.prompt_snapshot == CHAT_PROMPT
    assert new_run.model == "mock-director"
    assert retried.context_snapshot_id == result.assistant_message.context_snapshot_id
    assert (await db_session.get(AgentRun, new_id)).input_data["text"] == before
    again = await chat.service.retry(chat.conversation.id, assistant.id, chat.owner)
    assert again.agent_run_id == new_id
    assert await count(db_session, AgentRun) == 2
    assert await count(db_session, MarketingMessage) == 2


async def test_invalid_result_terminal_failure(db_session, chat):
    result = await turn(chat)
    service = AgentRunService(db_session)
    run_id = result.assistant_message.agent_run_id
    await service.claim(run_id)
    await service.finish_success(run_id, RuntimeResult({"message": ""}, 1, 1, 1, 2, None))
    await db_session.refresh(chat.conversation)
    await db_session.refresh(chat.owner)
    messages = await chat.service.messages(chat.conversation.id, chat.owner)
    assert messages[-1].status is Status.FAILED


async def test_queue_failure_placeholder_failed(db_session, chat, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Queue unavailable")

    monkeypatch.setattr(execute_agent_run, "apply_async", broken)
    result = await turn(chat)
    assert result.assistant_message.status is Status.FAILED
    assert (
        await db_session.get(Task, result.assistant_message.task_id)
    ).status is TaskStatus.FAILED


async def test_recovery_marks_stale_pending_failed(db_session, chat):
    result = await turn(chat)
    runtime = AgentRunService(db_session)
    run_id = result.assistant_message.agent_run_id
    await runtime.claim(run_id)
    run = await db_session.get(AgentRun, run_id)
    run.started_at = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds + 10)
    await db_session.commit()
    assert len(await TaskRecoveryService(db_session).recover_stuck()) == 1
    assert (await chat.service.messages(chat.conversation.id, chat.owner))[
        -1
    ].status is Status.FAILED


async def test_api_access_auth_internal_task_guard(client, db_session, chat):
    result = await turn(chat)
    app.dependency_overrides[get_current_user] = lambda: chat.owner
    try:
        assert (
            await client.get(f"/api/v1/tasks/{result.assistant_message.task_id}")
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/agent-runs/{result.assistant_message.agent_run_id}")
        ).status_code == 200
        stranger = User(id=uuid4(), role=UserRole.MANAGER)
        app.dependency_overrides[get_current_user] = lambda: stranger
        for path in [
            f"/api/v1/marketing-conversations/{chat.conversation.id}",
            f"/api/v1/agent-runs/{result.assistant_message.agent_run_id}",
        ]:
            assert (await client.get(path)).status_code == 404
        assert (await client.get("/api/v1/agent-runs")).json() == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_bounded_extractive_context(monkeypatch):
    monkeypatch.setattr(settings, "director_chat_recent_messages", 12)
    conversation = MarketingConversation()
    history = [
        MarketingMessage(role=Role.USER, content=f"exact message {i} " + "x" * 1500)
        for i in range(40)
    ]
    text = build_context(
        conversation,
        history,
        {"campaign": {"name": "Large"}, "articles": [{"title": "x" * 9000}] * 100},
        "Current",
    )
    assert len(text) <= settings.director_chat_context_max_chars
    assert len(conversation.context_digest) <= settings.director_chat_digest_max_chars
    assert "exact message 27" in conversation.context_digest
    assert "exact message 39" in text
    assert "CONVERSATION DIGEST" in text and "RECENT MESSAGES" in text


async def test_seed_preserves_custom_chat_and_strategy_prompts(db_session, chat):
    from app.seed import seed_agents

    chat.agent.settings = {"chat_prompt": "Customized chat", "other": "preserve"}
    original = chat.agent.system_prompt
    await db_session.commit()
    await seed_agents()
    await db_session.refresh(chat.agent)
    assert chat.agent.settings == {"chat_prompt": "Customized chat", "other": "preserve"}
    assert chat.agent.system_prompt == original
    chat.agent.settings = {"other": "preserve"}
    await db_session.commit()
    await seed_agents()
    await db_session.refresh(chat.agent)
    assert chat.agent.settings["chat_prompt"] == CHAT_PROMPT
    assert chat.agent.settings["other"] == "preserve"


async def test_operational_counts_and_dispatch_exclude_internal(db_session, chat):
    from app.api.system import system_status
    from app.services.task_dispatcher_service import TaskDispatcherService

    before = await system_status(chat.owner, db_session)
    await turn(chat)
    after = await system_status(chat.owner, db_session)
    assert before["tasks"] == after["tasks"]
    assert before["ready_auto_ai_tasks"] == after["ready_auto_ai_tasks"]
    assert await TaskDispatcherService(db_session).dispatch_ready_tasks() == []
    assert len(chat.calls) == 1


async def test_real_runner_uses_chat_schema_and_no_tools(db_session, chat, monkeypatch):
    from app.services.agent_runner_service import AgentRunnerService

    monkeypatch.setattr(settings, "openai_api_key", "test-mock-only")
    monkeypatch.setattr(settings, "openai_agents_disable_tracing", True)
    result = await turn(chat)
    claimed = await AgentRunService(db_session).claim(result.assistant_message.agent_run_id)
    seen = []

    async def mock_sdk(agent, value, **kwargs):
        seen.append(agent)
        assert agent.tools == []
        assert agent.output_type is DirectorChatReply
        usage = SimpleNamespace(requests=1, input_tokens=2, output_tokens=3, total_tokens=5)
        return SimpleNamespace(
            final_output=DirectorChatReply(message="Mock Director response"),
            context_wrapper=SimpleNamespace(usage=usage),
            raw_responses=[],
        )

    monkeypatch.setattr("app.services.agent_runner_service.Runner.run", mock_sdk)
    output = await AgentRunnerService().run(*claimed[:3], claimed[3])
    assert output.output_data["message"] == "Mock Director response"
    assert len(seen) == 1
    await AgentRunService(db_session).finish_success(result.assistant_message.agent_run_id, output)
    assert (await chat.service.messages(chat.conversation.id, chat.owner))[
        -1
    ].status is Status.COMPLETED


async def test_stale_unsubmitted_queue_reconciles_to_failed(db_session, chat):
    result = await turn(chat)
    run = await db_session.get(AgentRun, result.assistant_message.agent_run_id)
    run.queue_job_id = None
    run.created_at = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds + 10)
    await db_session.commit()
    assert (await chat.service.messages(chat.conversation.id, chat.owner))[
        -1
    ].status is Status.FAILED


async def test_api_authenticated_send_load_and_archive(client, chat):
    app.dependency_overrides[get_current_user] = lambda: chat.owner
    root = f"/api/v1/marketing-conversations/{chat.conversation.id}"
    try:
        payload = {"content": "API question", "client_message_id": str(uuid4())}
        response = await client.post(root + "/messages", json=payload)
        assert response.status_code == 202
        assert response.json()["assistant_message"]["status"] == "PENDING"
        assert (await client.post(root + "/messages", json=payload)).json() == response.json()
        assert len((await client.get(root + "/messages")).json()) == 2
        assert (await client.post(root + "/archive")).status_code == 200
        assert (
            await client.post(
                root + "/messages", json={**payload, "client_message_id": str(uuid4())}
            )
        ).status_code == 409
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_concurrent_retry_creates_one_fresh_run(db_session, chat):
    result = await turn(chat)
    run_id = result.assistant_message.agent_run_id
    runtime = AgentRunService(db_session)
    await runtime.claim(run_id)
    await runtime.finish_failure(run_id, AgentRuntimeError("AGENT_TIMEOUT", "safe"))
    conversation_id, message_id, owner_id = (
        chat.conversation.id,
        result.assistant_message.id,
        chat.owner.id,
    )
    await db_session.commit()

    async def retry():
        async with async_session_factory() as session:
            user = await session.get(User, owner_id)
            message = await DirectorChatService(session).retry(conversation_id, message_id, user)
            return message.agent_run_id

    ids = await asyncio.gather(retry(), retry())
    assert ids[0] == ids[1] and ids[0] != run_id
    assert await count(db_session, AgentRun) == 2
    assert await count(db_session, MarketingMessage) == 2


async def test_failed_api_retry_serializes_updated_timestamp(client, db_session, chat):
    result = await turn(chat)
    runtime = AgentRunService(db_session)
    await runtime.claim(result.assistant_message.agent_run_id)
    await runtime.finish_failure(
        result.assistant_message.agent_run_id, AgentRuntimeError("AGENT_TIMEOUT", "safe")
    )
    app.dependency_overrides[get_current_user] = lambda: chat.owner
    path = (
        f"/api/v1/marketing-conversations/{chat.conversation.id}/messages/"
        f"{result.assistant_message.id}/retry"
    )
    try:
        first = await client.post(path)
        second = await client.post(path)
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()
        assert first.json()["status"] == "PENDING"
        assert first.json()["updated_at"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_worker_unexpected_failure_marks_assistant_failed(db_session, chat, monkeypatch):
    from app.workers.agent_worker import _execute

    result = await turn(chat)
    conversation_id, owner_id = chat.conversation.id, chat.owner.id

    async def unexpected(*args, **kwargs):
        raise RuntimeError("mock unexpected error")

    monkeypatch.setattr("app.workers.agent_worker.AgentRunnerService.run", unexpected)
    monkeypatch.setattr("app.workers.agent_worker.report_exception", lambda *args, **kwargs: None)
    await _execute(result.assistant_message.agent_run_id)
    async with async_session_factory() as session:
        owner = await session.get(User, owner_id)
        messages = await DirectorChatService(session).messages(conversation_id, owner)
        assert messages[-1].status is Status.FAILED
        assert messages[-1].error_code == "AGENT_RUNTIME_ERROR"
        task = await session.get(Task, messages[-1].task_id)
        assert task.status is TaskStatus.FAILED


def test_send_and_retry_share_ai_rate_limit():
    from starlette.requests import Request

    from app.core.rate_limit import SimpleRateLimitMiddleware

    for path in [
        "/api/v1/marketing-conversations/id/messages",
        "/api/v1/marketing-conversations/id/messages/message/retry",
    ]:
        request = Request({"type": "http", "path": path, "method": "POST", "headers": []})
        assert SimpleRateLimitMiddleware._category(request) == (
            "ai",
            settings.rate_limit_ai_actions_per_minute,
        )
