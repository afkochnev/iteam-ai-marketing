import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.content import request_revision
from app.core.database import async_session_factory
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus, ToolCall, ToolCallStatus
from app.models.approval import Approval, ApprovalStatus
from app.models.content import ContentItem, ContentType, ContentVersion, ContentVersionSource
from app.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
)
from app.models.knowledge_pack import KnowledgePack, KnowledgePackItem, KnowledgePackStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.schemas.approval import RequiredApprovalComment
from app.schemas.task import TaskCreate
from app.services.activity_log_service import ActivityLogService
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import RuntimeResult
from app.services.approval_service import ApprovalService
from app.services.task_dispatcher_service import AUTO_TASK_TYPES, TaskDispatcherService
from app.services.task_service import TaskService
from app.tests.test_smm_integration import smm_fixture


@pytest.mark.asyncio
async def test_dispatcher_claims_ready_social_task_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _campaign, _article = await smm_fixture(db_session)
    calls: list[str] = []

    async def enqueue(self: AgentRunService, run):  # type: ignore[no-untyped-def]
        calls.append(str(run.id))
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", enqueue)
    service = TaskDispatcherService(db_session)
    first = await service.dispatch_ready_tasks()
    second = await service.dispatch_ready_tasks()
    assert first == [task.id]
    assert second == []
    assert len(calls) == 1
    assert await db_session.scalar(select(Task).where(Task.id == task.id))
    run = await db_session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
    assert run is not None


@pytest.mark.asyncio
async def test_dispatcher_two_postgres_sessions_claim_once(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production SELECT FOR UPDATE SKIP LOCKED path has one winner."""
    task, _campaign, _article = await smm_fixture(db_session)
    calls: list[str] = []

    async def enqueue(self: AgentRunService, run):  # type: ignore[no-untyped-def]
        calls.append(str(run.id))
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", enqueue)

    async def run_dispatch() -> list:
        async with async_session_factory() as session:
            return await TaskDispatcherService(session).dispatch_ready_tasks(limit=1)

    first, second = await asyncio.gather(run_dispatch(), run_dispatch())
    assert sorted(first + second, key=str) == [task.id]
    assert len(calls) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task.id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_dispatcher_enqueue_failure_is_retryable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _campaign, _article = await smm_fixture(db_session)
    from app.workers import agent_worker

    def fail(_run_id: str):
        raise RuntimeError("temporary broker outage")

    monkeypatch.setattr(agent_worker.execute_agent_run, "delay", fail)
    assert await TaskDispatcherService(db_session).dispatch_ready_tasks() == []
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.READY
    failed_run = await db_session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
    assert failed_run is not None and failed_run.status is AgentRunStatus.FAILED

    monkeypatch.setattr(
        agent_worker.execute_agent_run, "delay", lambda _run_id: type("Job", (), {"id": "job"})()
    )
    assert await TaskDispatcherService(db_session).dispatch_ready_tasks() == [task.id]


@pytest.mark.asyncio
async def test_concurrent_revision_requests_create_one_task(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task, _campaign, article_version = await smm_fixture(db_session)
    article_task = await TaskService(db_session).get_task(
        (await db_session.scalar(select(Task).where(Task.task_type == TaskType.WRITE_ARTICLE))).id
    )
    article = await db_session.scalar(
        select(ContentItem).where(ContentItem.current_version_id == article_version.id)
    )
    assert article is not None
    owner = await db_session.scalar(select(User))
    assert owner is not None
    await ApprovalService(db_session).create_content_approval(
        article.id,
        1,
        {
            "content_item_id": str(article.id),
            "content_version_id": str(article_version.id),
            "version_number": 1,
            "title": article.title,
            "content_hash": "hash",
        },
        article_task.assigned_agent_id,
    )
    await db_session.commit()
    monkeypatch.setattr(
        TaskDispatcherService, "dispatch_ready_tasks", lambda self: asyncio.sleep(0)
    )
    approval_id = await db_session.scalar(
        select(Approval.id).where(
            Approval.object_id == article.id, Approval.status == ApprovalStatus.PENDING
        )
    )
    assert approval_id is not None

    async def invoke():
        async with async_session_factory() as session:
            user = await session.get(User, owner.id)
            assert user is not None
            return await request_revision(
                article.id, RequiredApprovalComment(comment="Усилить CTA"), user, session
            )

    results = await asyncio.gather(invoke(), invoke(), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    async with async_session_factory() as check:
        assert (
            await check.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.task_type == TaskType.CONTENT_REVISION)
            )
            == 1
        )
        assert (
            await check.scalar(select(Approval.status).where(Approval.id == approval_id))
            is ApprovalStatus.REVISION_REQUESTED
        )


@pytest.mark.asyncio
async def test_dispatcher_excludes_manual_blocked_and_terminal(
    db_session: AsyncSession,
) -> None:
    task, _campaign, _article = await smm_fixture(db_session)
    assert TaskType.CREATE_SOCIAL_POSTS in AUTO_TASK_TYPES
    assert TaskType.MANUAL not in AUTO_TASK_TYPES
    blocked = await TaskService(db_session).get_task(task.id)
    blocked.status = TaskStatus.BLOCKED
    await db_session.commit()
    assert await TaskDispatcherService(db_session).dispatch_ready_tasks() == []


def test_dispatcher_allowlist_is_explicit() -> None:
    assert AUTO_TASK_TYPES == {
        TaskType.KNOWLEDGE_RESEARCH,
        TaskType.WRITE_ARTICLE,
        TaskType.CREATE_SOCIAL_POSTS,
        TaskType.CONTENT_REVISION,
    }


@pytest.mark.asyncio
async def test_activity_event_is_idempotent(db_session: AsyncSession) -> None:
    _task, campaign, _article = await smm_fixture(db_session)
    first = await ActivityLogService(db_session).record(
        "TASK_AUTO_DISPATCHED", operation_key="dispatch:test", campaign_id=campaign.id
    )
    second = await ActivityLogService(db_session).record(
        "TASK_AUTO_DISPATCHED", operation_key="dispatch:test", campaign_id=campaign.id
    )
    await db_session.commit()
    assert first.id == second.id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.operation_key == "dispatch:test")
        )
        == 1
    )


@pytest.mark.asyncio
async def test_article_revision_insufficient_is_atomic(db_session: AsyncSession) -> None:
    _smm_task, campaign, article_version = await smm_fixture(db_session)
    article = await db_session.scalar(
        select(ContentItem).where(ContentItem.current_version_id == article_version.id)
    )
    writer = await db_session.scalar(select(Agent).where(Agent.slug == "writer"))
    owner = await db_session.scalar(select(User))
    assert article is not None and writer is not None and owner is not None
    db_session.add(AgentTool(agent_id=writer.id, tool_name="read_knowledge_pack", is_enabled=True))
    source = KnowledgeSource(
        name="Research source",
        source_type=KnowledgeSourceType.FILE_UPLOAD,
        status=KnowledgeSourceStatus.ACTIVE,
        metadata_={},
    )
    db_session.add(source)
    await db_session.flush()
    knowledge_item = KnowledgeItem(
        source_id=source.id,
        title="Evidence",
        content_type="md",
        status=KnowledgeItemStatus.READY,
        created_by=owner.id,
        metadata_={},
    )
    db_session.add(knowledge_item)
    await db_session.flush()
    revision = await TaskService(db_session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.CONTENT_REVISION,
            title="Revise article",
            assigned_agent_id=writer.id,
            input_data={
                "content_item_id": str(article.id),
                "base_content_version_id": str(article_version.id),
                "approval_id": str(uuid4()),
                "revision_comment": "Усилить CTA",
                "revision_target_type": ContentType.ARTICLE.value,
            },
        )
    )
    dummy_run = AgentRun(
        agent_id=writer.id,
        task_id=revision.id,
        campaign_id=campaign.id,
        status=AgentRunStatus.COMPLETED,
        input_data={},
        prompt_snapshot="test",
        prompt_hash="test",
        model="test-model",
    )
    db_session.add(dummy_run)
    await db_session.flush()
    source_call = ToolCall(
        agent_run_id=dummy_run.id,
        tool_name="search_knowledge",
        arguments={},
        result={},
        status=ToolCallStatus.COMPLETED,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(source_call)
    await db_session.flush()
    pack = KnowledgePack(
        campaign_id=campaign.id,
        task_id=revision.id,
        agent_run_id=dummy_run.id,
        created_by_agent_id=writer.id,
        status=KnowledgePackStatus.READY,
        research_query="query",
        summary="summary",
        gaps=[],
        metadata_={},
    )
    db_session.add(pack)
    await db_session.flush()
    pack_item = KnowledgePackItem(
        knowledge_pack_id=pack.id,
        knowledge_item_id=knowledge_item.id,
        tool_call_id=source_call.id,
        result_key="a" * 64,
        source_id=source.id,
        source_title="Evidence",
        filename="evidence.md",
        file_id="file-evidence",
        excerpt="Evidence",
        position=1,
        metadata_={},
    )
    db_session.add(pack_item)
    await db_session.flush()
    db_session.add(
        ContentVersionSource(
            content_version_id=article_version.id,
            knowledge_pack_item_id=pack_item.id,
            section_key="problem",
            position=1,
        )
    )
    await db_session.flush()
    service = AgentRunService(db_session)
    run = await service.create_queued_run(revision.id)
    assert await service.claim(run.id)
    db_session.add(
        ToolCall(
            agent_run_id=run.id,
            tool_name="read_knowledge_pack",
            arguments={"knowledge_pack_id": str(pack.id)},
            result={"pack_id": str(pack.id), "items": []},
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(
            {
                "sufficient": False,
                "article": None,
                "gaps": ["Недостаточно подтверждённых материалов."],
            },
            1,
            1,
            1,
            2,
            None,
        ),
    )
    assert (await TaskService(db_session).get_task(revision.id)).status is TaskStatus.FAILED
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentVersion)
            .where(ContentVersion.content_item_id == article.id)
        )
        == 1
    )
