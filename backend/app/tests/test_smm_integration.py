import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.factory import AgentRuntimeContext
from app.agents.social_tools import read_content_version
from app.api.content import approve_content, reject_content
from app.core.config import settings
from app.core.database import async_session_factory, create_worker_session_factory
from app.core.errors import AppError
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus, ToolCall, ToolCallStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.marketing_feedback import FeedbackAnalysisStatus
from app.models.publication import Publication
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.schemas.campaign import CampaignCreate
from app.schemas.content import ContentApprovalRequest, ContentRejectionRequest
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import RuntimeResult
from app.services.approval_service import ApprovalService
from app.services.campaign_service import CampaignService
from app.services.feedback_service import FeedbackService
from app.services.task_dispatcher_service import TaskDispatcherService
from app.services.task_result_processors import result_processor_registry
from app.services.task_service import TaskService


def social_result(
    version_id: str, *, sufficient: bool = True, post_count: int = 5
) -> dict[str, object]:
    if not sufficient:
        return {
            "sufficient": False,
            "pack": None,
            "gaps": ["Недостаточно подтверждённых материалов."],
        }
    posts = [
        {
            "key": f"post_{index}",
            "channel": "TELEGRAM" if index % 2 else "VK",
            "title": f"Пост {index}",
            "text_markdown": f"Подтверждённый текст {index}",
            "cta": "Узнать больше",
            "sources": [{"content_version_id": version_id, "section_key": "problem"}],
            "suggested_publish_order": index,
        }
        for index in range(1, post_count + 1)
    ]
    return {
        "sufficient": True,
        "pack": {"strategy_summary": "Контентная серия", "posts": posts},
    }


async def smm_fixture(session: AsyncSession) -> tuple[Task, Campaign, ContentVersion]:
    owner = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Owner",
        role=UserRole.ADMIN,
    )
    writer = Agent(
        name="Writer",
        slug="writer",
        role="content_writer",
        system_prompt="Write grounded content.",
        model="test-model",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    smm = Agent(
        name="SMM Manager",
        slug="smm_manager",
        role="smm_manager",
        system_prompt="Adapt the article.",
        model="test-model",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    session.add_all([writer, smm])
    await session.flush()
    session.add(AgentTool(agent_id=smm.id, tool_name="read_content_version", is_enabled=True))
    campaign = await CampaignService(session).create_campaign(
        CampaignCreate(name="SMM integration campaign", goal="Create social content"),
        owner,
    )
    campaign.status = CampaignStatus.ACTIVE
    campaign.strategy = {
        "social_strategy": {"channels": ["TELEGRAM", "VK"], "post_count": 5},
    }
    article_task = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.WRITE_ARTICLE,
            title="Approved article",
            assigned_agent_id=writer.id,
        )
    )
    article_task.status = TaskStatus.COMPLETED
    article_item = ContentItem(
        campaign_id=campaign.id,
        source_task_id=article_task.id,
        content_type=ContentType.ARTICLE,
        title="Approved article",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=writer.id,
        metadata_={},
    )
    session.add(article_item)
    await session.flush()
    article_version = ContentVersion(
        content_item_id=article_item.id,
        version_number=1,
        content="# Article",
        structured_content={
            "title": "Approved article",
            "lead": "Lead",
            "sections": [{"key": "problem", "heading": "Problem", "body_markdown": "Evidence"}],
            "conclusion": "Conclusion",
            "cta": "CTA",
        },
        created_by_agent_id=writer.id,
        generation_key="article",
    )
    session.add(article_version)
    await session.flush()
    article_item.current_version_id = article_version.id
    article_task.output_data = {
        "content_item_id": str(article_item.id),
        "content_version_id": str(article_version.id),
        "content_type": "ARTICLE",
    }
    article_approval = await ApprovalService(session).create_content_approval(
        article_item.id,
        1,
        {
            "content_item_id": str(article_item.id),
            "content_version_id": str(article_version.id),
            "version_number": 1,
        },
        writer.id,
    )
    article_approval.status = ApprovalStatus.APPROVED
    article_item.status = ContentStatus.APPROVED
    social_task = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.CREATE_SOCIAL_POSTS,
            title="Create posts",
            assigned_agent_id=smm.id,
            dependency_ids=[article_task.id],
            input_data={
                "brief": "Adapt article into social posts",
                "strategy_version": campaign.strategy_version,
                "source_content_version_id": str(article_version.id),
            },
        )
    )
    await session.commit()
    return social_task, campaign, article_version


async def exhausted_smm_fixture(
    session: AsyncSession,
) -> tuple[Task, Campaign, ContentVersion, AgentRun]:
    task, campaign, version = await smm_fixture(session)
    historical = await AgentRunService(session).create_queued_run(task.id)
    historical.status = AgentRunStatus.FAILED
    historical.error_code = "AGENT_TIMEOUT"
    historical.error_message = "Превышено время выполнения агента."
    historical.completed_at = datetime.now(UTC)
    task.status = TaskStatus.FAILED
    task.retry_count = settings.agent_max_retries
    task.error_message = historical.error_message
    task.started_at = None
    task.completed_at = historical.completed_at
    await session.commit()
    return task, campaign, version, historical


async def add_read_audit(session: AsyncSession, run_id: UUID, version: ContentVersion) -> None:
    session.add(
        ToolCall(
            agent_run_id=run_id,
            tool_name="read_content_version",
            arguments={"content_version_id": str(version.id)},
            result={
                "content_version_id": str(version.id),
                "title": "Approved article",
                "section_keys": ["problem"],
                "content_hash": "hash",
            },
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def add_plan_item(
    session: AsyncSession,
    campaign: Campaign,
    article_version: ContentVersion,
    *,
    channel: ContentChannel = ContentChannel.TELEGRAM,
) -> tuple[PublicationPlan, PublicationPlanItem]:
    user = await session.scalar(select(User))
    assert user is not None
    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=datetime.now(UTC),
        planning_horizon_end=datetime.now(UTC) + timedelta(days=7),
        timezone_policy="UTC",
        created_by_user_id=user.id,
        approved_at=datetime.now(UTC),
        approved_by_user_id=user.id,
    )
    session.add(plan)
    await session.flush()
    item = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=1,
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
        channel=channel,
        source_content_item_id=article_version.content_item_id,
        source_content_version_id=article_version.id,
        topic="Почему решения не входят в ежедневную работу",
        angle="Показать разрыв между решением и исполнением",
        purpose="Помочь руководителю увидеть недостающий механизм",
        format="diagnostic",
        message_brief="Один grounded пост по утверждённой статье.",
        source_claim_ids=["article_test_p01"],
        source_support_summary="Проверочный источник.",
        status=PublicationPlanItemStatus.PLANNED,
    )
    session.add(item)
    await session.flush()
    return plan, item


@pytest.mark.asyncio
async def test_smm_waits_for_exact_article_approval_then_unlocks(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    article = await db_session.get(ContentItem, article_version.content_item_id)
    assert article is not None
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == article.id,
            Approval.subject_version == article_version.version_number,
        )
    )
    assert approval is not None
    approval.status = ApprovalStatus.PENDING
    article.status = ContentStatus.WAITING_APPROVAL
    task.status = TaskStatus.BLOCKED
    await db_session.commit()

    assert await TaskDispatcherService(db_session).dispatch_ready_tasks() == []
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).create_queued_run(task.id)
    assert error.value.code == "TASK_NOT_READY"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task.id)
        )
        == 0
    )

    approval.status = ApprovalStatus.APPROVED
    article.status = ContentStatus.APPROVED
    await TaskService(db_session).refresh_dependents_for_content(article.id)
    await db_session.commit()
    refreshed = await TaskService(db_session).get_task(task.id)
    assert refreshed.status is TaskStatus.READY
    run = await AgentRunService(db_session).create_queued_run(task.id)
    assert run.input_data["allowed_content_version_ids"] == [str(article_version.id)]


@pytest.mark.asyncio
async def test_smm_claim_carries_invocation_local_db_factory(
    db_session: AsyncSession,
) -> None:
    """SMM tools must use the factory owned by the current worker loop."""

    task, _campaign, _article_version = await smm_fixture(db_session)
    run = await AgentRunService(db_session).create_queued_run(task.id)
    worker_engine, worker_factory = create_worker_session_factory()
    try:
        claimed = await AgentRunService(db_session, worker_factory).claim(run.id)
        assert claimed is not None
        _snapshot, _input, context, _trace = claimed
        assert context.session_factory is worker_factory
    finally:
        await worker_engine.dispose()


@pytest.mark.asyncio
async def test_operator_recovery_requeues_exhausted_smm_without_duplicate_history(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _campaign, version, historical = await exhausted_smm_fixture(db_session)

    async def enqueue_without_broker(_service: AgentRunService, run: AgentRun) -> AgentRun:
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", enqueue_without_broker)
    recovered = await AgentRunService(db_session).recover_exhausted_smm_task(task.id)

    assert recovered.id != historical.id
    assert recovered.status is AgentRunStatus.QUEUED
    assert recovered.task_id == task.id
    assert task.status is TaskStatus.READY
    assert task.retry_count == settings.agent_max_retries
    assert task.error_message is None
    assert task.input_data["source_content_version_id"] == str(version.id)
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task.id)
        )
        == 2
    )

    with pytest.raises(AppError) as duplicate:
        await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert duplicate.value.code == "SMM_RECOVERY_NOT_APPLICABLE"


@pytest.mark.asyncio
async def test_operator_recovery_allows_fixed_validation_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _campaign, version, historical = await exhausted_smm_fixture(db_session)
    historical.error_code = "INVALID_SOCIAL_POST_RESULT"
    historical.error_message = "Посты не соответствуют стратегии кампании."
    task.error_message = historical.error_message
    await db_session.commit()

    async def enqueue_without_broker(_service: AgentRunService, run: AgentRun) -> AgentRun:
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", enqueue_without_broker)
    recovered = await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert recovered.status is AgentRunStatus.QUEUED
    assert task.input_data["source_content_version_id"] == str(version.id)
    assert task.error_message is None


@pytest.mark.asyncio
async def test_operator_recovery_rejects_stale_strategy_version(
    db_session: AsyncSession,
) -> None:
    task, campaign, _version, historical = await exhausted_smm_fixture(db_session)
    task.input_data = {
        **task.input_data,
        "strategy_version": campaign.strategy_version + 1,
    }
    historical.error_code = "INVALID_SOCIAL_POST_RESULT"
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert error.value.code == "SMM_STRATEGY_VERSION_STALE"


@pytest.mark.asyncio
async def test_operator_recovery_rejects_persisted_smm_result(
    db_session: AsyncSession,
) -> None:
    task, campaign, version, _historical = await exhausted_smm_fixture(db_session)
    result = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.SOCIAL_POST_PACK,
        title="Existing pack",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=task.assigned_agent_id,
        metadata_={},
    )
    db_session.add(result)
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert error.value.code == "SMM_RECOVERY_ALREADY_PERSISTED"
    assert version.id == UUID(task.input_data["source_content_version_id"])


@pytest.mark.asyncio
async def test_operator_recovery_rejects_unapproved_source_version(
    db_session: AsyncSession,
) -> None:
    task, _campaign, version, _historical = await exhausted_smm_fixture(db_session)
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == version.content_item_id,
            Approval.subject_version == version.version_number,
        )
    )
    assert approval is not None
    approval.status = ApprovalStatus.PENDING
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert error.value.code == "SMM_SOURCE_VERSION_NOT_APPROVED"


@pytest.mark.asyncio
async def test_operator_recovery_rejects_wrong_source_version(
    db_session: AsyncSession,
) -> None:
    task, _campaign, _version, _historical = await exhausted_smm_fixture(db_session)
    task.input_data = {**task.input_data, "source_content_version_id": str(uuid4())}
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert error.value.code == "SMM_SOURCE_VERSION_NOT_APPROVED"


@pytest.mark.asyncio
async def test_operator_recovery_rejects_active_agent_run(
    db_session: AsyncSession,
) -> None:
    task, _campaign, _version, _historical = await exhausted_smm_fixture(db_session)
    task.status = TaskStatus.READY
    active = await AgentRunService(db_session).create_queued_run(task.id)
    task.status = TaskStatus.FAILED
    task.retry_count = settings.agent_max_retries
    await db_session.commit()
    assert active.status is AgentRunStatus.QUEUED
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).recover_exhausted_smm_task(task.id)
    assert error.value.code == "TASK_ALREADY_QUEUED_OR_RUNNING"


async def _read_article_in_fresh_worker_loop(
    version_id: UUID, run_id: UUID, task_id: UUID, campaign_id: UUID
) -> str:
    """Exercise the real FunctionTool/database path in a new loop."""

    async def invoke() -> str:
        worker_engine, worker_factory = create_worker_session_factory()
        try:
            context = ToolContext(
                AgentRuntimeContext(
                    uuid4(),
                    task_id,
                    campaign_id,
                    run_id,
                    TaskType.CREATE_SOCIAL_POSTS,
                    allowed_content_version_ids=(version_id,),
                    session_factory=worker_factory,
                ),
                tool_name="read_content_version",
                tool_call_id=str(uuid4()),
                tool_arguments=json.dumps({"content_version_id": str(version_id)}),
            )
            result = await read_content_version.on_invoke_tool(
                context, json.dumps({"content_version_id": str(version_id)})
            )
            assert isinstance(result, str)
            return result
        finally:
            await worker_engine.dispose()

    return await invoke()


@pytest.mark.asyncio
async def test_smm_article_tool_is_safe_across_sequential_worker_loops(
    db_session: AsyncSession,
) -> None:
    """Two prefork-style deliveries must not reuse asyncpg state from loop A."""

    task, campaign, article_version = await smm_fixture(db_session)
    run = await AgentRunService(db_session).create_queued_run(task.id)
    first = await asyncio.to_thread(
        lambda: asyncio.run(
            _read_article_in_fresh_worker_loop(article_version.id, run.id, task.id, campaign.id)
        )
    )
    second = await asyncio.to_thread(
        lambda: asyncio.run(
            _read_article_in_fresh_worker_loop(article_version.id, run.id, task.id, campaign.id)
        )
    )
    assert json.loads(first)["content_version_id"] == str(article_version.id)
    assert json.loads(second)["content_version_id"] == str(article_version.id)


@pytest.mark.asyncio
async def test_smm_wrong_article_version_approval_does_not_unlock(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    article = await db_session.get(ContentItem, article_version.content_item_id)
    assert article is not None
    second = ContentVersion(
        content_item_id=article.id,
        version_number=2,
        content="# New article",
        structured_content=article_version.structured_content,
        created_by_agent_id=article_version.created_by_agent_id,
        generation_key="article:revision:test",
    )
    db_session.add(second)
    await db_session.flush()
    approval = await ApprovalService(db_session).create_content_approval(
        article.id,
        2,
        {"content_item_id": str(article.id), "content_version_id": str(second.id)},
        article.author_agent_id,
    )
    task.input_data = {**task.input_data, "source_content_version_id": str(second.id)}
    task.status = TaskStatus.BLOCKED
    await db_session.commit()
    await TaskService(db_session).refresh_dependents_for_content(article.id)
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.BLOCKED
    approval.status = ApprovalStatus.APPROVED
    article.status = ContentStatus.APPROVED
    await TaskService(db_session).refresh_dependents_for_content(article.id)
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.READY


@pytest.mark.asyncio
async def test_plan_item_task_without_article_dependency_is_runnable_and_idempotent(
    db_session: AsyncSession,
) -> None:
    bulk_task, campaign, article_version = await smm_fixture(db_session)
    plan, plan_item = await add_plan_item(db_session, campaign, article_version)
    bulk_task.status = TaskStatus.CANCELLED
    blocked = Task(
        campaign_id=campaign.id,
        task_type=TaskType.CREATE_SOCIAL_POSTS,
        title="Historical blocked plan-item task",
        assigned_agent_id=bulk_task.assigned_agent_id,
        status=TaskStatus.BLOCKED,
        input_data={
            "publication_plan_id": str(plan.id),
            "publication_plan_item_id": str(plan_item.id),
            "source_content_item_id": str(article_version.content_item_id),
            "source_content_version_id": str(article_version.id),
        },
    )
    db_session.add(blocked)
    await db_session.commit()

    payload = TaskCreate(
        campaign_id=campaign.id,
        task_type=TaskType.CREATE_SOCIAL_POSTS,
        title="Create one plan item post",
        assigned_agent_id=bulk_task.assigned_agent_id,
        input_data={
            "publication_plan_id": str(plan.id),
            "publication_plan_item_id": str(plan_item.id),
            "source_content_item_id": str(article_version.content_item_id),
            "source_content_version_id": str(article_version.id),
        },
    )
    reused = await TaskService(db_session).create_task(payload)
    assert reused.id == blocked.id
    assert reused.status is TaskStatus.READY
    duplicate = await TaskService(db_session).create_task(payload)
    assert duplicate.id == blocked.id

    run = await AgentRunService(db_session).create_queued_run(blocked.id)
    assert run.status is AgentRunStatus.QUEUED
    assert run.input_data["allowed_content_version_ids"] == [str(article_version.id)]
    assert run.input_data["publication_plan_item_id"] == str(plan_item.id)


@pytest.mark.asyncio
async def test_plan_item_business_state_overrides_historical_task_dependency(
    db_session: AsyncSession,
) -> None:
    bulk_task, campaign, article_version = await smm_fixture(db_session)
    plan, plan_item = await add_plan_item(db_session, campaign, article_version)
    bulk_task.status = TaskStatus.CANCELLED
    stale_dependency = Task(
        campaign_id=campaign.id,
        task_type=TaskType.WRITE_ARTICLE,
        title="Old unfinished article workflow",
        assigned_agent_id=None,
        status=TaskStatus.BLOCKED,
    )
    db_session.add(stale_dependency)
    await db_session.flush()
    task = await TaskService(db_session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.CREATE_SOCIAL_POSTS,
            title="Create grounded plan item post",
            assigned_agent_id=bulk_task.assigned_agent_id,
            dependency_ids=[stale_dependency.id],
            input_data={
                "publication_plan_id": str(plan.id),
                "publication_plan_item_id": str(plan_item.id),
                "source_content_item_id": str(article_version.content_item_id),
                "source_content_version_id": str(article_version.id),
            },
        )
    )
    assert task.status is TaskStatus.READY
    assert task.dependencies[0].depends_on_task.id == stale_dependency.id


@pytest.mark.asyncio
async def test_plan_item_wrong_source_is_rejected_before_task_creation(
    db_session: AsyncSession,
) -> None:
    bulk_task, campaign, article_version = await smm_fixture(db_session)
    plan, plan_item = await add_plan_item(db_session, campaign, article_version)
    bulk_task.status = TaskStatus.CANCELLED
    with pytest.raises(AppError) as error:
        await TaskService(db_session).create_task(
            TaskCreate(
                campaign_id=campaign.id,
                task_type=TaskType.CREATE_SOCIAL_POSTS,
                title="Wrong source",
                assigned_agent_id=bulk_task.assigned_agent_id,
                input_data={
                    "publication_plan_id": str(plan.id),
                    "publication_plan_item_id": str(plan_item.id),
                    "source_content_item_id": str(article_version.content_item_id),
                    "source_content_version_id": str(uuid4()),
                },
            )
        )
    assert error.value.code == "PUBLICATION_PLAN_SOURCE_INVALID"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.input_data["publication_plan_item_id"].astext == str(plan_item.id))
        )
        == 0
    )


@pytest.mark.asyncio
async def test_plan_item_smm_binds_authoritative_vk_channel(
    db_session: AsyncSession,
) -> None:
    task, campaign, article_version = await smm_fixture(db_session)
    user = await db_session.scalar(select(User))
    assert user is not None
    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=datetime.now(UTC),
        planning_horizon_end=datetime.now(UTC) + timedelta(days=7),
        timezone_policy="UTC",
        created_by_user_id=user.id,
        approved_at=datetime.now(UTC),
        approved_by_user_id=user.id,
    )
    db_session.add(plan)
    await db_session.flush()
    plan_item = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=1,
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
        channel=ContentChannel.VK,
        source_content_item_id=article_version.content_item_id,
        source_content_version_id=article_version.id,
        topic="Как выбрать формат работы",
        angle="Различить стратегическую и сценарную неопределённость",
        purpose="Помочь руководителю выбрать формат обсуждения",
        format="diagnostic",
        message_brief=("Показать, где именно находится неопределённость управленческой команды."),
        source_claim_ids=["article_test_p01"],
        source_support_summary="Проверочный источник.",
        status=PublicationPlanItemStatus.PLANNED,
    )
    db_session.add(plan_item)
    await db_session.flush()
    task.input_data = {
        **task.input_data,
        "publication_plan_id": str(plan.id),
        "publication_plan_item_id": str(plan_item.id),
    }
    await db_session.commit()

    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert task.input_data["plan_channel"] == "VK"
    claimed = await service.claim(run.id)
    assert claimed is not None
    snapshot, task_input, _context, _trace = claimed
    assert snapshot.output_type.__name__ == "SingleSocialPostResult"
    assert "plan_channel" in task_input and "VK" in task_input
    assert "SingleSocialPostResult" in task_input
    assert "Не добавляй channel" in task_input
    assert "SocialPostPackResult" not in task_input
    assert "allowed channels" not in task_input
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()

    output = {
        "sufficient": True,
        "pack": {
            "strategy_summary": "Один пост по утверждённому пункту плана.",
            "posts": [
                {
                    "key": "plan_item_post",
                    "title": "Как выбрать формат работы",
                    "text_markdown": (
                        "Когда неопределённость относится к будущим сценариям, "
                        "сначала нужно разобрать варианты развития событий."
                    ),
                    "cta": "",
                    "sources": [
                        {
                            "content_version_id": str(article_version.id),
                            "section_key": "problem",
                        }
                    ],
                    "suggested_publish_order": 1,
                }
            ],
        },
    }
    await service.finish_success(run.id, RuntimeResult(output, 1, 10, 5, 15, None))

    persisted_run = await service.get_run(run.id)
    assert persisted_run.status is AgentRunStatus.COMPLETED
    post = await db_session.scalar(
        select(ContentItem).where(
            ContentItem.source_task_id == task.id,
            ContentItem.content_type == ContentType.SOCIAL_POST,
        )
    )
    assert post is not None
    assert post.channel is ContentChannel.VK
    assert post.current_version_id is not None
    version = await db_session.get(ContentVersion, post.current_version_id)
    assert version is not None
    assert version.structured_content["channel"] == "VK"
    assert (
        version.structured_content["text_markdown"] == output["pack"]["posts"][0]["text_markdown"]
    )
    derivation = await db_session.scalar(
        select(ContentDerivation).where(ContentDerivation.derived_content_version_id == version.id)
    )
    assert derivation is not None
    assert derivation.source_content_version_id == article_version.id
    assert derivation.source_section_key == "problem"
    assert post.status is ContentStatus.WAITING_APPROVAL
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.object_id == post.id,
                Approval.status == ApprovalStatus.PENDING,
            )
        )
        == 1
    )
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 0


@pytest.mark.asyncio
async def test_smm_success_persists_pack_posts_versions_and_derivations(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.COMPLETED
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    items = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.source_task_id == task.id)
            )
        ).all()
    )
    packs = [item for item in items if item.content_type is ContentType.SOCIAL_POST_PACK]
    posts = [item for item in items if item.content_type is ContentType.SOCIAL_POST]
    assert len(packs) == 1 and len(posts) == 5
    pack = packs[0]
    assert pack.status is ContentStatus.WAITING_APPROVAL
    assert all(post.status is ContentStatus.WAITING_APPROVAL for post in posts)
    assert all(post.parent_content_item_id == pack.id for post in posts)
    assert all(len(post.versions) == 1 for post in posts)
    assert len(pack.versions) == 1
    assert all(version.source_agent_run_id == run.id for post in posts for version in post.versions)
    derivations = list((await db_session.scalars(select(ContentDerivation))).all())
    assert len(derivations) == 5
    assert all(item.source_content_version_id == article_version.id for item in derivations)
    approval_count = await db_session.scalar(
        select(func.count())
        .select_from(Approval)
        .where(
            Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    assert approval_count == 1
    run_row = await service.get_run(run.id)
    task_row = await TaskService(db_session).get_task(task.id)
    await result_processor_registry.get(task_row.task_type).process(
        db_session, run_row, task_row, social_result(str(article_version.id))
    )
    await db_session.commit()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.source_task_id == task.id)
        )
        == 6
    )
    assert await db_session.scalar(select(func.count()).select_from(ContentDerivation)) == 5


@pytest.mark.asyncio
async def test_smm_without_read_fails_before_persisting_content(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.FAILED
    failed_run = await service.get_run(run.id)
    assert failed_run.error_code == "CONTENT_VERSION_NOT_READ"
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
async def test_smm_strategy_rejects_six_posts_when_nine_are_required(
    db_session: AsyncSession,
) -> None:
    task, campaign, article_version = await smm_fixture(db_session)
    campaign.strategy = {"social_strategy": {"channels": ["TELEGRAM", "VK"], "post_count": 9}}
    task.input_data = {**task.input_data, "strategy_version": campaign.strategy_version}
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id), post_count=6), 1, 10, 5, 15, None),
    )
    failed_run = await service.get_run(run.id)
    assert failed_run.status is AgentRunStatus.FAILED
    assert failed_run.error_code == "INVALID_SOCIAL_POST_RESULT"
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
async def test_smm_insufficient_completes_run_but_fails_task_without_content(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id), sufficient=False), 1, 10, 5, 15, None),
    )
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.FAILED
    assert stored_task.output_data["error_code"] == "INSUFFICIENT_SOCIAL_SOURCE"
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unknown_version", "unknown_section"])
async def test_smm_rejects_hallucinated_social_provenance(
    db_session: AsyncSession, mode: str
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    output = social_result(str(article_version.id))
    pack = output["pack"]
    assert isinstance(pack, dict)
    posts = pack["posts"]
    assert isinstance(posts, list) and posts
    source = posts[0]["sources"][0]
    if mode == "unknown_version":
        source["content_version_id"] = str(uuid4())
    else:
        source["section_key"] = "imaginary_section"
    await service.finish_success(run.id, RuntimeResult(output, 1, 10, 5, 15, None))
    failed_run = await service.get_run(run.id)
    assert failed_run.status is AgentRunStatus.FAILED
    assert failed_run.error_code in {
        "INVALID_SOCIAL_SOURCE",
        "INVALID_SOCIAL_SOURCE_SECTION",
    }
    assert failed_run.request_count == 1
    assert failed_run.input_tokens == 10
    assert failed_run.output_tokens == 5
    assert failed_run.total_tokens == 15
    accounting = failed_run.input_data["model_request_accounting"]
    assert accounting["final_validation_state"] == failed_run.error_code
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
async def test_pack_approval_is_safe_for_concurrent_requests(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    pack = await db_session.scalar(
        select(ContentItem).where(
            ContentItem.source_task_id == task.id,
            ContentItem.content_type == ContentType.SOCIAL_POST_PACK,
        )
    )
    user = await db_session.scalar(select(User))
    assert pack is not None and user is not None
    pack_id = pack.id
    user_id = user.id

    async def approve_once() -> None:
        async with async_session_factory() as session:
            approver = await session.get(User, user_id)
            assert approver is not None
            await approve_content(
                pack_id,
                ContentApprovalRequest(comment="Согласовано"),
                approver,
                session,
            )

    results = await asyncio.gather(approve_once(), approve_once(), return_exceptions=True)
    assert all(not isinstance(result, Exception) for result in results)
    await db_session.refresh(pack)
    assert pack.status is ContentStatus.APPROVED
    children = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
            )
        ).all()
    )
    assert len(children) == 5
    assert all(child.status is ContentStatus.APPROVED for child in children)
    approvals = list(
        (
            await db_session.scalars(
                select(Approval).where(
                    Approval.object_id == pack.id,
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                )
            )
        ).all()
    )
    assert len(approvals) == 1 and approvals[0].status is ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_social_post_quality_blocks_forbidden_format_at_approval(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    pack = await db_session.scalar(
        select(ContentItem).where(
            ContentItem.source_task_id == task.id,
            ContentItem.content_type == ContentType.SOCIAL_POST_PACK,
        )
    )
    user = await db_session.scalar(select(User))
    assert pack is not None and user is not None
    child = await db_session.scalar(
        select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
    )
    assert child is not None and child.current_version_id is not None
    version = await db_session.get(ContentVersion, child.current_version_id)
    assert version is not None
    original = version.content
    version.content = "**Неподходящий текст**"
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == pack.id,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    assert approval is not None
    snapshot_posts = [dict(post) for post in approval.subject_snapshot["posts"]]
    snapshot_post = next(
        post for post in snapshot_posts if post["content_item_id"] == str(child.id)
    )
    snapshot_post["content_hash"] = hashlib.sha256(version.content.encode()).hexdigest()
    approval.subject_snapshot = {**approval.subject_snapshot, "posts": snapshot_posts}
    await db_session.commit()

    with pytest.raises(AppError) as error:
        await approve_content(
            pack.id, ContentApprovalRequest(comment="Согласовано"), user, db_session
        )
    assert error.value.code == "SOCIAL_POST_QUALITY_INVALID"
    assert version.content == "**Неподходящий текст**"
    assert version.content != original


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", ["**жирный текст**", "CTA: ответьте", "Порядок: 1"])
async def test_feedback_recommendations_cannot_bypass_smm_quality_validation(
    db_session: AsyncSession, forbidden: str
) -> None:
    task, campaign, article_version = await smm_fixture(db_session)
    user = await db_session.scalar(select(User))
    assert user is not None
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    analysis.recommendations = [
        {
            "category": "FORMAT",
            "recommendation": f"Используй {forbidden}",
            "evidence_refs": [],
            "expected_effect": "Больше внимания",
            "priority": "high",
        }
    ]
    analysis.status = FeedbackAnalysisStatus.ACCEPTED
    await db_session.commit()
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id, feedback_analysis_id=analysis.id)
    assert run.input_data["feedback_analysis_snapshot"]["analysis_id"] == str(analysis.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    output = social_result(str(article_version.id))
    posts = output["pack"]["posts"]
    assert isinstance(posts, list)
    posts[0]["text_markdown"] = forbidden
    await service.finish_success(run.id, RuntimeResult(output, 1, 10, 5, 15, None))
    await db_session.refresh(run)
    assert run.status is AgentRunStatus.FAILED
    assert run.error_code == "INVALID_SOCIAL_POST_RESULT"
    persisted = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.source_task_id == task.id)
            )
        ).all()
    )
    assert persisted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["pack", "child"])
async def test_pack_approval_rejects_stale_pack_or_child_version(
    db_session: AsyncSession, target: str
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    pack = await db_session.scalar(
        select(ContentItem).where(
            ContentItem.source_task_id == task.id,
            ContentItem.content_type == ContentType.SOCIAL_POST_PACK,
        )
    )
    user = await db_session.scalar(select(User))
    assert pack is not None and user is not None and pack.current_version_id is not None
    if target == "pack":
        replacement = ContentVersion(
            content_item_id=pack.id,
            version_number=2,
            content="new pack",
            structured_content={},
            created_by_agent_id=pack.author_agent_id,
            generation_key="pack-replacement",
        )
    else:
        child = await db_session.scalar(
            select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
        )
        assert child is not None
        replacement = ContentVersion(
            content_item_id=child.id,
            version_number=2,
            content="new post",
            structured_content={},
            created_by_agent_id=child.author_agent_id,
            generation_key="post-replacement",
        )
    db_session.add(replacement)
    await db_session.flush()
    if target == "pack":
        pack.current_version_id = replacement.id
    else:
        assert child is not None
        child.current_version_id = replacement.id
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await approve_content(
            pack.id, ContentApprovalRequest(comment="Согласовано"), user, db_session
        )
    assert error.value.code == "STALE_APPROVAL_VERSION"


async def _social_post_with_pending_approval(
    db_session: AsyncSession,
) -> tuple[ContentItem, ContentVersion, Approval, Task, User]:
    task, campaign, _article_version = await smm_fixture(db_session)
    user = await db_session.scalar(select(User))
    assert user is not None
    post = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.SOCIAL_POST,
        title="Диагностический пост",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=task.assigned_agent_id,
        channel="TELEGRAM",
        metadata_={},
    )
    db_session.add(post)
    await db_session.flush()
    first = ContentVersion(
        content_item_id=post.id,
        version_number=1,
        content="Первая версия поста.",
        structured_content={"text_markdown": "Первая версия поста."},
        created_by_agent_id=task.assigned_agent_id,
        generation_key="test-post-v1",
    )
    db_session.add(first)
    await db_session.flush()
    post.current_version_id = first.id
    first_approval = await ApprovalService(db_session).create_content_approval(
        post.id,
        1,
        {
            "content_item_id": str(post.id),
            "content_version_id": str(first.id),
            "version_number": 1,
            "content_hash": hashlib.sha256(first.content.encode()).hexdigest(),
        },
        task.assigned_agent_id,
    )
    await db_session.commit()
    return post, first, first_approval, task, user


@pytest.mark.asyncio
async def test_new_social_post_revision_supersedes_pending_approval(
    db_session: AsyncSession,
) -> None:
    post, first, old_approval, task, _user = await _social_post_with_pending_approval(db_session)
    second = ContentVersion(
        content_item_id=post.id,
        version_number=2,
        content="Вторая версия поста.",
        structured_content={"text_markdown": "Вторая версия поста."},
        created_by_agent_id=task.assigned_agent_id,
        generation_key="test-post-v2",
    )
    db_session.add(second)
    await db_session.flush()
    post.current_version_id = second.id
    new_approval = await ApprovalService(db_session).create_content_approval(
        post.id,
        2,
        {
            "content_item_id": str(post.id),
            "content_version_id": str(second.id),
            "version_number": 2,
            "content_hash": hashlib.sha256(second.content.encode()).hexdigest(),
        },
        task.assigned_agent_id,
    )
    await db_session.commit()
    await db_session.refresh(old_approval)
    assert first.content == "Первая версия поста."
    assert old_approval.status is ApprovalStatus.REVISION_REQUESTED
    assert old_approval.metadata_["superseded_by_content_version_id"] == str(second.id)
    assert new_approval.status is ApprovalStatus.PENDING
    assert post.current_version_id == second.id
    assert post.status is ContentStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_stale_approval_is_rejected_without_approving_current_version(
    db_session: AsyncSession,
) -> None:
    post, _first, approval, task, user = await _social_post_with_pending_approval(db_session)
    second = ContentVersion(
        content_item_id=post.id,
        version_number=2,
        content="Вторая версия поста.",
        structured_content={"text_markdown": "Вторая версия поста."},
        created_by_agent_id=task.assigned_agent_id,
        generation_key="test-post-v2-stale",
    )
    db_session.add(second)
    await db_session.flush()
    post.current_version_id = second.id
    # Deliberately preserve a stale pending approval to exercise the defense.
    with pytest.raises(AppError) as error:
        await approve_content(
            post.id,
            ContentApprovalRequest(comment="Согласовано"),
            user,
            db_session,
        )
    assert error.value.code == "STALE_APPROVAL_VERSION"
    await db_session.rollback()
    await db_session.refresh(post)
    assert post.status is ContentStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_current_version_approval_requires_exact_hash_and_approves(
    db_session: AsyncSession,
) -> None:
    post, _first, _old_approval, task, user = await _social_post_with_pending_approval(db_session)
    second = ContentVersion(
        content_item_id=post.id,
        version_number=2,
        content="Вторая версия поста.",
        structured_content={"text_markdown": "Вторая версия поста."},
        created_by_agent_id=task.assigned_agent_id,
        generation_key="test-post-v2-current",
    )
    db_session.add(second)
    await db_session.flush()
    post.current_version_id = second.id
    await ApprovalService(db_session).create_content_approval(
        post.id,
        2,
        {
            "content_item_id": str(post.id),
            "content_version_id": str(second.id),
            "version_number": 2,
            "content_hash": hashlib.sha256(second.content.encode()).hexdigest(),
        },
        task.assigned_agent_id,
    )
    response = await approve_content(
        post.id,
        ContentApprovalRequest(comment="Согласовано"),
        user,
        db_session,
    )
    assert response.approved_version_id == second.id
    assert response.current_version is not None
    assert response.current_version.id == second.id
    assert any(
        approval.status == "APPROVED" and approval.subject_version == 2
        for approval in response.approval_history
    )
    await db_session.commit()
    await db_session.refresh(post)
    assert post.status is ContentStatus.APPROVED
    approved = list(
        (
            await db_session.scalars(
                select(Approval).where(
                    Approval.object_id == post.id,
                    Approval.status == ApprovalStatus.APPROVED,
                )
            )
        ).all()
    )
    assert len(approved) == 1
    assert approved[0].subject_snapshot["content_version_id"] == str(second.id)
    assert approved[0].subject_snapshot["version_number"] == 2


@pytest.mark.asyncio
async def test_smm_requires_read_for_every_allowed_article_version(
    db_session: AsyncSession,
) -> None:
    task, campaign, article_version = await smm_fixture(db_session)
    second_item = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.ARTICLE,
        title="Second article",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=task.assigned_agent_id,
        metadata_={},
    )
    db_session.add(second_item)
    await db_session.flush()
    second_version = ContentVersion(
        content_item_id=second_item.id,
        version_number=1,
        content="# Second",
        structured_content={"sections": []},
        created_by_agent_id=task.assigned_agent_id,
        generation_key="second-article",
    )
    db_session.add(second_version)
    await db_session.flush()
    second_item.current_version_id = second_version.id
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    run.input_data["allowed_content_version_ids"] = [
        str(article_version.id),
        str(second_version.id),
    ]
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    failed_run = await service.get_run(run.id)
    assert failed_run.error_code == "CONTENT_VERSION_NOT_READ"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.source_task_id == task.id)
        )
        == 1
    )


async def create_article_approval(session: AsyncSession) -> tuple[ContentItem, User]:
    article = await session.scalar(
        select(ContentItem).where(ContentItem.content_type == ContentType.ARTICLE)
    )
    user = await session.scalar(select(User))
    assert article is not None and user is not None and article.current_version_id is not None
    version = next(
        version for version in article.versions if version.id == article.current_version_id
    )
    await ApprovalService(session).create_content_approval(
        article.id,
        version.version_number,
        {
            "content_item_id": str(article.id),
            "content_version_id": str(version.id),
            "version_number": version.version_number,
            "title": article.title,
            "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
        },
        article.author_agent_id,
    )
    await session.commit()
    return article, user


@pytest.mark.asyncio
async def test_article_approval_can_approve_and_reject_with_authenticated_user(
    db_session: AsyncSession,
) -> None:
    await smm_fixture(db_session)
    article, user = await create_article_approval(db_session)
    approved = await approve_content(
        article.id, ContentApprovalRequest(comment="Согласовано"), user, db_session
    )
    assert approved.status is ContentStatus.APPROVED
    assert approved.current_version is not None
    assert approved.approved_version_id == approved.current_version.id
    article, user = await create_article_approval(db_session)
    with pytest.raises(AppError):
        await reject_content(article.id, ContentRejectionRequest(comment=" "), user, db_session)
