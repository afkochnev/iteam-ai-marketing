from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.content import approve_content, request_revision
from app.core.config import settings
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus, ToolCall, ToolCallStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
    ContentVersionSource,
)
from app.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
    KnowledgeStore,
    KnowledgeStoreProvider,
    KnowledgeStoreStatus,
)
from app.models.knowledge_pack import KnowledgePack, KnowledgePackItem, KnowledgePackStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.schemas.content import ContentApprovalRequest
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import AgentRuntimeError, RuntimeResult
from app.services.approval_service import ApprovalService
from app.services.campaign_planning_service import CampaignPlanningService
from app.services.knowledge_search_service import build_result_key
from app.services.task_dispatcher_service import TaskDispatcherService
from app.services.task_result_processors import result_processor_registry
from app.services.task_service import TaskService
from app.tests.test_campaign_planning import complete_plan, setup_campaign
from app.tests.test_smm_integration import smm_fixture


def _social_output(article_version_id: str) -> dict[str, object]:
    posts = []
    for index in range(1, 6):
        posts.append(
            {
                "key": f"post_{index}",
                "channel": "TELEGRAM" if index % 2 else "VK",
                "title": f"Пост {index}",
                "text_markdown": f"Текст поста {index}",
                "cta": "Узнать больше",
                "sources": [{"content_version_id": article_version_id, "section_key": "problem"}],
                "suggested_publish_order": index,
            }
        )
    return {
        "sufficient": True,
        "pack": {"strategy_summary": "Серия публикаций", "posts": posts},
    }


def _article_revision_output(pack_item_id: str) -> dict[str, object]:
    return {
        "sufficient": True,
        "gaps": [],
        "article": {
            "title": "Обновлённая статья",
            "subtitle": "Новая версия",
            "lead": "Проверенный лид",
            "sections": [
                {
                    "key": "problem",
                    "heading": "Проблема",
                    "body_markdown": "Подтверждённый текст",
                    "knowledge_pack_item_ids": [pack_item_id],
                },
                {
                    "key": "solution",
                    "heading": "Решение",
                    "body_markdown": "Практическое решение",
                    "knowledge_pack_item_ids": [pack_item_id],
                },
                {
                    "key": "conclusion",
                    "heading": "Вывод",
                    "body_markdown": "Итог",
                    "knowledge_pack_item_ids": [pack_item_id],
                },
            ],
            "conclusion": "Итог",
            "cta": "Узнать больше",
        },
    }


@pytest.mark.asyncio
async def test_dispatcher_runs_knowledge_writer_and_smm_without_manual_runs(
    db_session: AsyncSession,
) -> None:
    user, campaign = await setup_campaign(db_session)
    await complete_plan(db_session, campaign.id)
    agents = {agent.slug: agent for agent in (await db_session.scalars(select(Agent))).all()}
    db_session.add_all(
        [
            AgentTool(agent_id=agents["knowledge_keeper"].id, tool_name="search_knowledge"),
            AgentTool(agent_id=agents["writer"].id, tool_name="read_knowledge_pack"),
            AgentTool(agent_id=agents["smm_manager"].id, tool_name="read_content_version"),
        ]
    )
    source = KnowledgeSource(
        name="Manual source",
        source_type=KnowledgeSourceType.FILE_UPLOAD,
        status=KnowledgeSourceStatus.ACTIVE,
        metadata_={},
    )
    db_session.add(source)
    await db_session.flush()
    item = KnowledgeItem(
        source_id=source.id,
        title="Management guide",
        content_type="md",
        original_filename="management.md",
        openai_file_id="file-management",
        status=KnowledgeItemStatus.READY,
        created_by=user.id,
        metadata_={},
    )
    db_session.add(item)
    db_session.add(
        KnowledgeStore(
            provider=KnowledgeStoreProvider.OPENAI,
            name="Test store",
            external_store_id=f"vs-{uuid4()}",
            status=KnowledgeStoreStatus.ACTIVE,
            is_active=True,
            metadata_={},
        )
    )
    await db_session.commit()
    await CampaignPlanningService(db_session).approve(campaign.id, user, "Strategy approved")
    manual_calls: list[str] = []
    knowledge_failed_once = False
    retry_countdowns: list[int] = []
    original_enqueue = AgentRunService.enqueue

    async def fake_enqueue(service: AgentRunService, run: AgentRun, *, countdown: int = 0):  # type: ignore[no-untyped-def]
        retry_countdowns.append(countdown)
        claimed = await service.claim(run.id)
        assert claimed is not None
        task = await service.session.get(Task, run.task_id)
        assert task is not None
        if task.task_type is TaskType.KNOWLEDGE_RESEARCH:
            nonlocal knowledge_failed_once
            if not knowledge_failed_once:
                knowledge_failed_once = True
                await service.finish_failure(
                    run.id, AgentRuntimeError("AGENT_TIMEOUT", "temporary provider timeout")
                )
                return run
            key = build_result_key(item.id, "file-management", "Материал об управляемости.")
            service.session.add(
                ToolCall(
                    agent_run_id=run.id,
                    tool_name="search_knowledge",
                    arguments={"query": "управляемость", "max_results": 10},
                    result={
                        "query": "управляемость",
                        "result_count": 1,
                        "results": [
                            {
                                "result_key": key,
                                "knowledge_item_id": str(item.id),
                                "source_id": str(item.source_id),
                                "source_title": "Manual source",
                                "filename": "management.md",
                                "file_id": "file-management",
                                "excerpt": "Материал об управляемости.",
                                "score": 0.9,
                                "metadata": {},
                            }
                        ],
                    },
                    status=ToolCallStatus.COMPLETED,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
            await service.session.commit()
            await service.finish_success(
                run.id,
                RuntimeResult(
                    {
                        "research_query": "управляемость",
                        "summary": "Проверенный материал найден.",
                        "sufficient": True,
                        "selected_results": [{"result_key": key, "selection_reason": "Релевантен"}],
                        "gaps": [],
                    },
                    1,
                    10,
                    10,
                    20,
                    None,
                ),
            )
        elif task.task_type is TaskType.WRITE_ARTICLE:
            pack = await service.session.scalar(
                select(KnowledgePack).where(
                    KnowledgePack.task_id.in_(
                        select(Task.id).where(Task.task_type == TaskType.KNOWLEDGE_RESEARCH)
                    )
                )
            )
            assert pack is not None and pack.items
            pack_item = pack.items[0]
            service.session.add(
                ToolCall(
                    agent_run_id=run.id,
                    tool_name="read_knowledge_pack",
                    arguments={"knowledge_pack_id": str(pack.id)},
                    result={
                        "pack_id": str(pack.id),
                        "items": [{"knowledge_pack_item_id": str(pack_item.id)}],
                    },
                    status=ToolCallStatus.COMPLETED,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
            await service.session.commit()
            await service.finish_success(
                run.id,
                RuntimeResult(
                    {
                        "sufficient": True,
                        "article": {
                            "title": "Статья об управляемости",
                            "subtitle": None,
                            "lead": "Вводная часть.",
                            "sections": [
                                {
                                    "key": "problem",
                                    "heading": "Проблема",
                                    "body_markdown": "Подтверждённый материал.",
                                    "knowledge_pack_item_ids": [str(pack_item.id)],
                                },
                                {
                                    "key": "solution",
                                    "heading": "Решение",
                                    "body_markdown": "Практический подход.",
                                    "knowledge_pack_item_ids": [str(pack_item.id)],
                                },
                                {
                                    "key": "result",
                                    "heading": "Результат",
                                    "body_markdown": "Ожидаемый результат.",
                                    "knowledge_pack_item_ids": [str(pack_item.id)],
                                },
                            ],
                            "conclusion": "Вывод.",
                            "cta": "Пройти диагностику",
                        },
                        "gaps": [],
                    },
                    1,
                    10,
                    10,
                    20,
                    None,
                ),
            )
        else:
            article_task = (
                await service.session.scalars(
                    select(Task).where(Task.task_type == TaskType.WRITE_ARTICLE)
                )
            ).first()
            assert article_task is not None
            article_version_id = article_task.output_data["content_version_id"]
            service.session.add(
                ToolCall(
                    agent_run_id=run.id,
                    tool_name="read_content_version",
                    arguments={"content_version_id": article_version_id},
                    result={
                        "content_version_id": article_version_id,
                        "title": "Статья об управляемости",
                        "section_keys": ["problem", "solution", "result"],
                        "content_hash": "hash",
                    },
                    status=ToolCallStatus.COMPLETED,
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
            await service.session.commit()
            await service.finish_success(
                run.id, RuntimeResult(_social_output(str(article_version_id)), 1, 10, 10, 20, None)
            )
        return run

    # The monkeypatch is deliberately at the queue boundary: dispatcher,
    # runtime claim, processors and all database transitions remain real.
    AgentRunService.enqueue = fake_enqueue  # type: ignore[method-assign]
    try:
        for _ in range(4):
            await TaskDispatcherService(db_session).dispatch_ready_tasks()
    finally:
        AgentRunService.enqueue = original_enqueue  # type: ignore[method-assign]

    tasks = list(
        (await db_session.scalars(select(Task).where(Task.campaign_id == campaign.id))).all()
    )
    assert {task.task_type for task in tasks} >= {
        TaskType.KNOWLEDGE_RESEARCH,
        TaskType.WRITE_ARTICLE,
        TaskType.CREATE_SOCIAL_POSTS,
    }
    assert all(
        task.status is TaskStatus.COMPLETED
        for task in tasks
        if task.task_type
        in {TaskType.KNOWLEDGE_RESEARCH, TaskType.WRITE_ARTICLE, TaskType.CREATE_SOCIAL_POSTS}
    )
    assert knowledge_failed_once
    assert settings.agent_retry_backoff_seconds in retry_countdowns
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.campaign_id == campaign.id)
        )
        == 5
    )
    knowledge_runs = list(
        (
            await db_session.scalars(
                select(AgentRun)
                .join(Task, Task.id == AgentRun.task_id)
                .where(Task.task_type == TaskType.KNOWLEDGE_RESEARCH)
                .order_by(AgentRun.created_at)
            )
        ).all()
    )
    assert len(knowledge_runs) == 2
    assert knowledge_runs[0].status is AgentRunStatus.FAILED
    assert knowledge_runs[0].error_code == "AGENT_TIMEOUT"
    assert knowledge_runs[1].status is AgentRunStatus.COMPLETED
    activity_events = {
        row.event_type
        for row in (
            await db_session.scalars(
                select(ActivityLog).where(ActivityLog.campaign_id == campaign.id)
            )
        ).all()
    }
    assert {"AGENT_RUN_FAILED", "TASK_RETRY_SCHEDULED"}.issubset(activity_events)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.campaign_id == campaign.id,
                ContentItem.content_type == ContentType.ARTICLE,
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.campaign_id == campaign.id,
                ContentItem.content_type == ContentType.SOCIAL_POST_PACK,
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.campaign_id == campaign.id,
                ContentItem.content_type == ContentType.SOCIAL_POST,
            )
        )
        == 5
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
        == 2
    )
    events = set(
        (
            await db_session.scalars(
                select(ActivityLog.event_type).where(ActivityLog.campaign_id == campaign.id)
            )
        ).all()
    )
    assert {
        "TASK_AUTO_DISPATCHED",
        "ARTICLE_CREATED",
        "SOCIAL_POST_PACK_CREATED",
        "KNOWLEDGE_PACK_CREATED",
    } <= events
    assert manual_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["success", "unauthorized_version", "invalid_section", "insufficient"]
)
async def test_pack_revision_lifecycle_and_atomic_failures(
    db_session: AsyncSession, mode: str
) -> None:
    task, campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    first = await service.create_queued_run(task.id)
    assert await service.claim(first.id)
    db_session.add(
        ToolCall(
            agent_run_id=first.id,
            tool_name="read_content_version",
            arguments={"content_version_id": str(article_version.id)},
            result={
                "content_version_id": str(article_version.id),
                "title": "Approved article",
                "section_keys": ["problem"],
                "content_hash": "hash",
            },
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await service.finish_success(
        first.id, RuntimeResult(_social_output(str(article_version.id)), 1, 1, 1, 2, None)
    )
    pack = await db_session.scalar(
        select(ContentItem).where(ContentItem.content_type == ContentType.SOCIAL_POST_PACK)
    )
    assert pack is not None
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == pack.id, Approval.status == ApprovalStatus.PENDING
        )
    )
    smm = await db_session.scalar(select(Agent).where(Agent.slug == "smm_manager"))
    assert approval is not None and smm is not None
    approval.status = ApprovalStatus.REVISION_REQUESTED
    revision = await TaskService(db_session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.CONTENT_REVISION,
            title="Revise social pack",
            assigned_agent_id=smm.id,
            input_data={
                "content_item_id": str(pack.id),
                "base_content_version_id": str(pack.current_version_id),
                "approval_id": str(approval.id),
                "revision_comment": "Обновить серию",
                "revision_target_type": ContentType.SOCIAL_POST_PACK.value,
            },
        )
    )
    await db_session.commit()
    second = await service.create_queued_run(revision.id)
    assert await service.claim(second.id)
    db_session.add(
        ToolCall(
            agent_run_id=second.id,
            tool_name="read_content_version",
            arguments={"content_version_id": str(article_version.id)},
            result={
                "content_version_id": str(article_version.id),
                "title": "Approved article",
                "section_keys": ["problem"],
                "content_hash": "hash",
            },
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    old_pack_version_id = pack.current_version_id
    old_children = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
            )
        ).all()
    )
    old_child_versions = {child.id: child.current_version_id for child in old_children}
    old_derivation_count = await db_session.scalar(
        select(func.count()).select_from(ContentDerivation)
    )
    revised = (
        {"sufficient": False, "pack": None, "gaps": ["Недостаточно материалов."]}
        if mode == "insufficient"
        else _social_output(str(article_version.id))
    )
    if mode != "insufficient":
        assert isinstance(revised["pack"], dict)
        revised_posts = revised["pack"]["posts"]
        assert isinstance(revised_posts, list)
        if mode == "unauthorized_version":
            revised_posts[0]["sources"][0]["content_version_id"] = str(uuid4())
        elif mode == "invalid_section":
            revised_posts[0]["sources"][0]["section_key"] = "missing"
        revised_posts[-1]["key"] = "post_6"
    await service.finish_success(second.id, RuntimeResult(revised, 1, 1, 1, 2, None))
    if mode != "success":
        failed_run = await service.get_run(second.id)
        failed_task = await TaskService(db_session).get_task(revision.id)
        if mode == "insufficient":
            assert failed_run.status is AgentRunStatus.COMPLETED
        else:
            assert failed_run.status is AgentRunStatus.FAILED
        assert failed_task.status is TaskStatus.FAILED
        expected_error = (
            "INSUFFICIENT_SOCIAL_SOURCE" if mode == "insufficient" else "INVALID_SOCIAL_SOURCE"
        )
        if mode == "insufficient":
            assert failed_task.output_data.get("error_code") == expected_error
        else:
            assert failed_run.error_code in {expected_error, "INVALID_SOCIAL_SOURCE_SECTION"}
        await db_session.refresh(pack)
        assert pack.current_version_id == old_pack_version_id
        current_children = list(
            (
                await db_session.scalars(
                    select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
                )
            ).all()
        )
        assert len(current_children) == len(old_children)
        assert {
            child.id: child.current_version_id for child in current_children
        } == old_child_versions
        assert all(child.status is ContentStatus.WAITING_APPROVAL for child in current_children)
        assert (
            await db_session.scalar(select(func.count()).select_from(ContentDerivation))
            == old_derivation_count
        )
        assert (
            await db_session.scalar(
                select(func.count()).select_from(Approval).where(Approval.object_id == pack.id)
            )
            == 1
        )
        return
    revision_task = await TaskService(db_session).get_task(revision.id)
    revision_run = await service.get_run(second.id)
    await result_processor_registry.get(revision_task.task_type).process(
        db_session, revision_run, revision_task, revised
    )
    await db_session.commit()
    await db_session.refresh(pack)
    children = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
            )
        ).all()
    )
    by_key: dict[str, ContentItem] = {}
    for child in children:
        current = await db_session.get(ContentVersion, child.current_version_id)
        if current:
            by_key[str(current.structured_content.get("key"))] = child
    assert len(children) == 6
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentVersion)
            .where(ContentVersion.content_item_id == pack.id)
        )
        == 2
    )
    assert by_key["post_5"].status is ContentStatus.ARCHIVED
    assert all(
        by_key[key].status is ContentStatus.WAITING_APPROVAL
        for key in ["post_1", "post_2", "post_3", "post_4", "post_6"]
    )
    approvals = list(
        (
            await db_session.scalars(
                select(Approval).where(Approval.object_id == pack.id).order_by(Approval.created_at)
            )
        ).all()
    )
    assert [entry.status for entry in approvals] == [
        ApprovalStatus.REVISION_REQUESTED,
        ApprovalStatus.PENDING,
    ]
    user = await db_session.scalar(select(User))
    assert user is not None
    await approve_content(
        pack.id,
        ContentApprovalRequest(comment="Пакет согласован"),
        user,
        db_session,
    )
    await db_session.commit()
    approvals = list(
        (
            await db_session.scalars(
                select(Approval).where(Approval.object_id == pack.id).order_by(Approval.created_at)
            )
        ).all()
    )
    assert approvals[0].status is ApprovalStatus.REVISION_REQUESTED
    assert approvals[1].status is ApprovalStatus.APPROVED
    await db_session.refresh(pack)
    assert pack.status is ContentStatus.APPROVED
    assert by_key["post_5"].status is ContentStatus.ARCHIVED
    assert all(
        by_key[key].status is ContentStatus.APPROVED
        for key in ["post_1", "post_2", "post_3", "post_4", "post_6"]
    )


@pytest.mark.asyncio
async def test_article_revision_success_and_idempotent_processing(
    db_session: AsyncSession,
) -> None:
    _smm_task, campaign, article_version = await smm_fixture(db_session)
    article = await db_session.scalar(
        select(ContentItem).where(ContentItem.current_version_id == article_version.id)
    )
    writer = await db_session.scalar(select(Agent).where(Agent.slug == "writer"))
    user = await db_session.scalar(select(User))
    assert article is not None and writer is not None and user is not None
    db_session.add(AgentTool(agent_id=writer.id, tool_name="read_knowledge_pack"))
    source = KnowledgeSource(
        name="Revision source",
        source_type=KnowledgeSourceType.FILE_UPLOAD,
        status=KnowledgeSourceStatus.ACTIVE,
        metadata_={},
    )
    db_session.add(source)
    await db_session.flush()
    item = KnowledgeItem(
        source_id=source.id,
        title="Evidence",
        content_type="md",
        status=KnowledgeItemStatus.READY,
        created_by=user.id,
        metadata_={},
    )
    db_session.add(item)
    await db_session.flush()
    research_task = await TaskService(db_session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.KNOWLEDGE_RESEARCH,
            title="Research",
            assigned_agent_id=writer.id,
        )
    )
    research_run = AgentRun(
        agent_id=writer.id,
        task_id=research_task.id,
        campaign_id=campaign.id,
        status=AgentRunStatus.COMPLETED,
        input_data={},
        prompt_snapshot="test",
        prompt_hash="test",
        model="test-model",
    )
    db_session.add(research_run)
    await db_session.flush()
    pack = KnowledgePack(
        campaign_id=campaign.id,
        task_id=research_task.id,
        agent_run_id=research_run.id,
        created_by_agent_id=writer.id,
        status=KnowledgePackStatus.READY,
        research_query="query",
        summary="summary",
        gaps=[],
        metadata_={},
    )
    db_session.add(pack)
    await db_session.flush()
    research_call = ToolCall(
        agent_run_id=research_run.id,
        tool_name="search_knowledge",
        arguments={},
        result={},
        status=ToolCallStatus.COMPLETED,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(research_call)
    await db_session.flush()
    pack_item = KnowledgePackItem(
        knowledge_pack_id=pack.id,
        knowledge_item_id=item.id,
        tool_call_id=research_call.id,
        result_key="b" * 64,
        source_id=source.id,
        source_title="Revision source",
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
    article.status = ContentStatus.WAITING_APPROVAL
    await ApprovalService(db_session).create_content_approval(
        article.id,
        1,
        {"content_item_id": str(article.id), "content_version_id": str(article_version.id)},
        writer.id,
    )
    await db_session.commit()
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == article.id, Approval.status == ApprovalStatus.PENDING
        )
    )
    assert approval is not None
    await request_revision(
        article.id,
        ContentApprovalRequest(comment="Усилить CTA"),
        user,
        db_session,
    )
    revision = await db_session.scalar(
        select(Task).where(Task.task_type == TaskType.CONTENT_REVISION)
    )
    assert revision is not None and revision.status is TaskStatus.READY
    service = AgentRunService(db_session)
    run = await db_session.scalar(select(AgentRun).where(AgentRun.task_id == revision.id))
    assert run is not None
    assert await service.claim(run.id)
    db_session.add(
        ToolCall(
            agent_run_id=run.id,
            tool_name="read_knowledge_pack",
            arguments={"knowledge_pack_id": str(pack.id)},
            result={
                "pack_id": str(pack.id),
                "items": [{"knowledge_pack_item_id": str(pack_item.id)}],
            },
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    output = _article_revision_output(str(pack_item.id))
    await service.finish_success(run.id, RuntimeResult(output, 1, 1, 1, 2, None))
    await db_session.refresh(article)
    assert article.current_version_id != article_version.id
    versions = list(
        (
            await db_session.scalars(
                select(ContentVersion).where(ContentVersion.content_item_id == article.id)
            )
        ).all()
    )
    assert len(versions) == 2
    approvals = list(
        (
            await db_session.scalars(
                select(Approval)
                .where(Approval.object_id == article.id)
                .order_by(Approval.created_at)
            )
        ).all()
    )
    assert [approval.status for approval in approvals] == [
        ApprovalStatus.REVISION_REQUESTED,
        ApprovalStatus.PENDING,
    ]
    task_row = await TaskService(db_session).get_task(revision.id)
    run_row = await service.get_run(run.id)
    await result_processor_registry.get(task_row.task_type).process(
        db_session, run_row, task_row, output
    )
    await db_session.commit()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentVersion)
            .where(ContentVersion.content_item_id == article.id)
        )
        == 2
    )
    await approve_content(
        article.id,
        ContentApprovalRequest(comment="Согласовано"),
        user,
        db_session,
    )
    await db_session.commit()
    approvals = list(
        (
            await db_session.scalars(
                select(Approval)
                .where(Approval.object_id == article.id)
                .order_by(Approval.created_at)
            )
        ).all()
    )
    assert approvals[0].status is ApprovalStatus.REVISION_REQUESTED
    assert approvals[1].status is ApprovalStatus.APPROVED
    await db_session.refresh(article)
    assert article.status is ContentStatus.APPROVED
