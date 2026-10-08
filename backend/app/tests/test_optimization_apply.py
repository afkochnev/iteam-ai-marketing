import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus, ToolCall, ToolCallStatus
from app.models.approval import Approval, ApprovalStatus
from app.models.campaign import CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentVersionSource,
)
from app.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemStatus,
    KnowledgeSource,
    KnowledgeSourceStatus,
    KnowledgeSourceType,
)
from app.models.knowledge_pack import (
    KnowledgePack,
    KnowledgePackItem,
    KnowledgePackStatus,
)
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.models.optimization import CampaignOptimizationAction, CampaignOptimizationProposal
from app.models.optimization import OptimizationActionStatus as AS
from app.models.optimization import OptimizationActionType as AT
from app.models.optimization import OptimizationProposalStatus as PS
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.schemas.optimization import OptimizationActionApplyRequest
from app.schemas.publication_plan import PlanItemInput, PublicationPlanCreate
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import RuntimeResult
from app.services.auth_service import AuthService
from app.services.campaign_planning_service import CampaignPlanningService
from app.services.feedback_service import FeedbackService
from app.services.optimization_proposal_service import OptimizationProposalService
from app.services.publication_plan_service import PublicationPlanService
from app.services.task_service import TaskService
from app.tests.test_agent_runtime import runtime_fixture
from app.tests.test_optimization import draft, recommendation, snapshot
from app.tests.test_smm_integration import smm_fixture
from app.workers.agent_worker import execute_agent_run
from app.workers.publication_plan_worker import generate_publication_plan


@pytest.fixture(autouse=True)
def offline_queues(monkeypatch):
    monkeypatch.setattr(
        execute_agent_run, "apply_async", lambda **kw: SimpleNamespace(id="offline-job")
    )
    monkeypatch.setattr(
        generate_publication_plan,
        "apply_async",
        lambda **kw: SimpleNamespace(id="offline-plan-job"),
    )
    monkeypatch.setattr(settings, "openai_default_model", "gpt-5.6")


async def setup_action(db_session, kind="CONTENT_REVISION", status=AS.APPROVED):
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
    director = Agent(
        name="Director",
        slug="marketing_director",
        role="director",
        system_prompt="Strategy",
        model="gpt-5.6",
        status="ACTIVE",
        settings={},
        autonomy_level=2,
    )
    db_session.add(director)
    campaign.strategy_version = 7
    campaign.strategy = {"campaign_summary": "Original approved strategy"}
    await db_session.commit()
    plan = await PublicationPlanService(db_session).create_plan(
        campaign.id,
        user,
        PublicationPlanCreate(
            planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
            planning_horizon_end=datetime(2026, 10, 14, tzinfo=UTC),
            timezone_policy="Europe/Bratislava",
            items=[
                PlanItemInput(
                    scheduled_at=datetime(2026, 10, 5, tzinfo=UTC),
                    channel=ContentChannel.TELEGRAM,
                    source_content_version_id=article_version.id,
                    topic="Topic",
                    angle="Angle",
                    purpose="Purpose",
                    format="observation",
                    message_brief="An exact original editorial brief",
                )
            ],
        ),
    )
    plan.status = PublicationPlanStatus.APPROVED
    plan.approved_at = datetime.now(UTC)
    s = snapshot(campaign.id)
    s["optimization_target_allowlist"]["content"] = [
        {
            "campaign_id": str(campaign.id),
            "content_item_id": str(article.id),
            "content_version_id": str(article_version.id),
        }
    ]
    s["optimization_target_allowlist"]["publication_plans"] = [
        {"campaign_id": str(campaign.id), "id": str(plan.id)}
    ]
    overrides = {}
    if kind == "CONTENT_REVISION":
        overrides = {
            "target_entity_type": "CONTENT_ITEM",
            "target_entity_id": str(article.id),
            "target_version_id": str(article_version.id),
        }
    if kind == "PUBLICATION_PLAN_REVISION":
        overrides = {"target_entity_type": "PUBLICATION_PLAN", "target_entity_id": str(plan.id)}
    analysis = MarketingFeedbackAnalysis(
        campaign_id=campaign.id,
        strategy_version=7,
        status=FeedbackAnalysisStatus.DRAFT,
        summary="Accepted frozen analysis",
        input_snapshot=s,
        findings=[],
        recommendations=[recommendation(draft(s, kind, **overrides))],
        experiment_ideas=[],
        limitations=[],
        agent_run_id=research_run.id,
    )
    db_session.add(analysis)
    await db_session.commit()
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    proposal = await db_session.scalar(
        select(CampaignOptimizationProposal).where(
            CampaignOptimizationProposal.feedback_analysis_id == analysis.id
        )
    )
    action = proposal.actions[0]
    if status != AS.PROPOSED:
        await OptimizationProposalService(db_session).decide(action.id, user, status)
    return user, campaign, article, article_version, plan, analysis, proposal, action


async def count_artifacts(session, action_id):
    return [
        await session.scalar(
            select(func.count()).select_from(model).where(model.optimization_action_id == action_id)
        )
        for model in [Task, PublicationPlan]
    ]


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
async def test_apply_provenance_idempotency_immutability(db_session, kind):
    user, campaign, item, version, old_plan, analysis, proposal, action = await setup_action(
        db_session, kind
    )
    original = (
        version.content,
        deepcopy(version.structured_content),
        campaign.strategy_version,
        deepcopy(campaign.strategy),
    )
    old_plan_snapshot = (old_plan.status, old_plan.approved_at)
    approvals = list(
        (
            await db_session.execute(
                text("SELECT id,status::text,subject_snapshot FROM approvals ORDER BY id")
            )
        ).all()
    )
    service = OptimizationProposalService(db_session)
    first = await service.apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Human-only comment")
    )
    second = await service.apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Different ignored comment")
    )
    assert first.artifact_id == second.artifact_id and first.artifact_type == second.artifact_type
    assert await count_artifacts(db_session, action.id) == (
        [0, 1] if kind == "PUBLICATION_PLAN_REVISION" else [1, 0]
    )
    await db_session.refresh(action)
    await db_session.refresh(proposal)
    await db_session.refresh(campaign)
    assert (
        action.status == AS.APPLIED and action.applied_by_user_id == user.id and action.applied_at
    )
    assert proposal.status == PS.APPLIED
    assert (
        version.content,
        version.structured_content,
        campaign.strategy_version,
        campaign.strategy,
    ) == original
    assert (old_plan.status, old_plan.approved_at) == old_plan_snapshot
    assert approvals == list(
        (
            await db_session.execute(
                text("SELECT id,status::text,subject_snapshot FROM approvals ORDER BY id")
            )
        ).all()
    )
    task = await db_session.get(Task, first.task_id)
    run = await db_session.get(AgentRun, first.agent_run_id)
    assert run and run.status == AgentRunStatus.QUEUED
    if kind == "PUBLICATION_PLAN_REVISION":
        plan = await db_session.get(PublicationPlan, first.publication_plan_id)
        assert plan.status == PublicationPlanStatus.DRAFT and plan.id != old_plan.id
        assert (
            plan.feedback_analysis_id,
            plan.optimization_proposal_id,
            plan.optimization_action_id,
        ) == (analysis.id, proposal.id, action.id)
        frozen = run.input_data["publication_plan_snapshot"]
        assert frozen["source_publication_plan"]["id"] == str(old_plan.id)
        assert frozen["planning_horizon_start"] == old_plan.planning_horizon_start.isoformat()
        assert frozen["channels"] == ["TELEGRAM"] and frozen["total_items"] == 1
        assert frozen["article_version_ids"] == [str(version.id)]
        assert frozen["accepted_feedback_analysis"]["analysis_id"] == str(analysis.id)
        assert frozen["approved_optimization_action"]["status"] == "APPROVED"
        assert frozen["optimization_action_id"] == str(action.id)
    else:
        assert task.optimization_action_id == action.id
        for key, value in {
            "optimization_action_id": action.id,
            "optimization_proposal_id": proposal.id,
            "accepted_analysis_id": analysis.id,
            "feedback_analysis_id": analysis.id,
        }.items():
            assert task.input_data[key] == str(value)
        assert task.input_data["human_comment"] == "Human-only comment"
        assert task.input_data["approved_optimization_action"]["id"] == str(action.id)
        if kind == "CONTENT_REVISION":
            assert task.task_type == TaskType.CONTENT_REVISION
            for key, value in {
                "source_content_item_id": item.id,
                "content_item_id": item.id,
                "source_content_version_id": version.id,
                "base_content_version_id": version.id,
            }.items():
                assert task.input_data[key] == str(value)
            assert task.input_data["revision_comment"] == "Human-only comment"
            assert task.input_data["immutable_source_context"]["knowledge_pack_ids"]
        else:
            assert task.task_type == TaskType.CAMPAIGN_PLANNING
            assert (
                task.input_data["strategy_version"] == 8
                and task.input_data["preserve_active_strategy"]
            )
            assert task.input_data["previous_strategy"] == original[3]
            assert "Accepted frozen analysis" in run.input_data["text"]
    events = list(
        await db_session.scalars(
            select(ActivityLog).where(ActivityLog.event_type == "OPTIMIZATION_ACTION_APPLIED")
        )
    )
    assert len(events) == 1 and events[0].user_id == user.id
    expected = {
        "proposal_id": str(proposal.id),
        "action_id": str(action.id),
        "feedback_analysis_id": str(analysis.id),
        "action_type": kind,
        "artifact_type": first.artifact_type,
        "artifact_id": str(first.artifact_id),
        "task_id": str(first.task_id),
    }
    if kind != "CONTENT_REVISION":
        expected["agent_run_id"] = str(first.agent_run_id)
    if kind == "PUBLICATION_PLAN_REVISION":
        expected["publication_plan_id"] = str(first.publication_plan_id)
    assert events[0].metadata_ == expected


@pytest.mark.parametrize(
    "status,code",
    [
        (AS.PROPOSED, "OPTIMIZATION_ACTION_NOT_APPROVED"),
        (AS.REJECTED, "OPTIMIZATION_ACTION_REJECTED"),
        (AS.FAILED, "OPTIMIZATION_ACTION_FAILED"),
    ],
)
async def test_apply_requires_approved(db_session, status, code):
    user, *_, action = await setup_action(db_session, status=AS.PROPOSED)
    action.status = status
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment="Human")
        )
    assert error.value.code == code and await count_artifacts(db_session, action.id) == [0, 0]


@pytest.mark.parametrize("kind", ["NO_CHANGE", "EXPERIMENT"])
async def test_actions_require_applicable_configuration(db_session, kind):
    user, *_, action = await setup_action(db_session, kind)
    with pytest.raises(AppError) as error:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest()
        )
    assert error.value.code == (
        "OPTIMIZATION_ACTION_NOT_APPLICABLE"
        if kind == "NO_CHANGE"
        else "EXPERIMENT_CONFIG_REQUIRED"
    )
    assert await count_artifacts(db_session, action.id) == [0, 0]


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
@pytest.mark.parametrize(
    "change,code", [("archive", "CAMPAIGN_ARCHIVED"), ("strategy", "OPTIMIZATION_CONTEXT_STALE")]
)
async def test_global_stale_archive_rejected(db_session, kind, change, code):
    user, campaign, *_, action = await setup_action(db_session, kind)
    if change == "archive":
        campaign.status = CampaignStatus.ARCHIVED
    else:
        campaign.strategy_version += 1
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment="Human")
        )
    assert error.value.code == code and await count_artifacts(db_session, action.id) == [0, 0]


@pytest.mark.parametrize(
    "change", ["current_version", "wrong_item", "wrong_version", "frozen_version"]
)
async def test_exact_content_target_guard(db_session, change):
    user, campaign, item, version, _, analysis, _, action = await setup_action(db_session)
    if change == "current_version":
        item.current_version_id = None
    elif change == "wrong_item":
        item.campaign_id = (await runtime_fixture(db_session))[2].id
    elif change == "wrong_version":
        action.target_version_id = uuid4()
    else:
        analysis.input_snapshot = {
            **analysis.input_snapshot,
            "optimization_target_allowlist": {
                **analysis.input_snapshot["optimization_target_allowlist"],
                "content": [],
            },
        }
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment="Human")
        )
    assert error.value.code in {"OPTIMIZATION_TARGET_STALE", "OPTIMIZATION_ACTION_INVALID"}
    assert await count_artifacts(db_session, action.id) == [0, 0]


@pytest.mark.parametrize("comment", [None, "", "  "])
async def test_content_human_comment_required(db_session, comment):
    user, *_, action = await setup_action(db_session)
    with pytest.raises(AppError) as error:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment=comment)
        )
    assert error.value.code == "OPTIMIZATION_HUMAN_COMMENT_REQUIRED"


@pytest.mark.parametrize("change", ["rejected", "replaced", "empty"])
async def test_plan_target_must_be_current_approved(db_session, change):
    user, campaign, _, _, plan, _, _, action = await setup_action(
        db_session, "PUBLICATION_PLAN_REVISION"
    )
    if change == "rejected":
        plan.status = PublicationPlanStatus.REJECTED
    elif change == "empty":
        for item in await db_session.scalars(
            select(PublicationPlanItem).where(PublicationPlanItem.publication_plan_id == plan.id)
        ):
            item.status = PublicationPlanItemStatus.REMOVED
    else:
        db_session.add(
            PublicationPlan(
                campaign_id=campaign.id,
                status=PublicationPlanStatus.APPROVED,
                planning_horizon_start=plan.planning_horizon_start,
                planning_horizon_end=plan.planning_horizon_end,
                timezone_policy=plan.timezone_policy,
                created_by_user_id=user.id,
                approved_at=datetime.now(UTC),
            )
        )
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest()
        )
    assert error.value.code == "OPTIMIZATION_TARGET_STALE"


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
async def test_concurrent_apply_postgres_guard(db_session, kind):
    user, _, _, _, _, _, _, action = await setup_action(db_session, kind)
    uid, aid = user.id, action.id

    async def apply():
        async with async_session_factory() as session:
            current = await session.get(User, uid)
            return await OptimizationProposalService(session).apply_action(
                aid, current, OptimizationActionApplyRequest(human_comment="Human")
            )

    results = await asyncio.gather(apply(), apply(), apply())
    assert len({result.artifact_id for result in results}) == 1
    assert await count_artifacts(db_session, aid) == (
        [0, 1] if kind == "PUBLICATION_PLAN_REVISION" else [1, 0]
    )


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
async def test_enqueue_failure_is_durable_and_not_repeated(db_session, monkeypatch, kind):
    user, *_, action = await setup_action(db_session, kind)
    calls = []

    def fail(**kw):
        calls.append(kw)
        raise RuntimeError("broker offline")

    monkeypatch.setattr(
        generate_publication_plan if kind == "PUBLICATION_PLAN_REVISION" else execute_agent_run,
        "apply_async",
        fail,
    )
    service = OptimizationProposalService(db_session)
    first = await service.apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Human")
    )
    assert first.status == "FAILED" and first.error_message and first.action.status == AS.APPLIED
    second = await service.apply_action(action.id, user, OptimizationActionApplyRequest())
    assert first.artifact_id == second.artifact_id and second.status == "FAILED" and len(calls) == 1
    task = await db_session.get(Task, first.task_id)
    run = await db_session.get(AgentRun, first.agent_run_id)
    assert task.status == TaskStatus.FAILED and run.status == AgentRunStatus.FAILED
    assert await count_artifacts(db_session, action.id) == (
        [0, 1] if kind == "PUBLICATION_PLAN_REVISION" else [1, 0]
    )


@pytest.mark.parametrize(
    "table,constraint",
    [
        ("tasks", "uq_tasks_optimization_action"),
        ("publication_plans", "uq_publication_plans_optimization_action"),
    ],
)
async def test_durable_unique_and_restrict_fk(db_session, table, constraint):
    user, *_, action = await setup_action(
        db_session, "STRATEGY_REVIEW" if table == "tasks" else "PUBLICATION_PLAN_REVISION"
    )
    result = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest()
    )
    original = await db_session.get(
        Task if table == "tasks" else PublicationPlan, result.artifact_id
    )
    values = {
        column.name: getattr(original, column.name)
        for column in original.__table__.columns
        if column.name not in {"id", "created_at", "updated_at"}
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(type(original)(**values))
            await db_session.flush()
    values["optimization_action_id"] = uuid4()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(type(original)(**values))
            await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.delete(action)
            await db_session.flush()
    assert (
        await db_session.scalar(text("SELECT version_num FROM alembic_version")) == "20261008_0027"
    )


async def test_api_auth_comment_response_reload(db_session, client):
    user, *_, action = await setup_action(db_session)
    url = f"/api/v1/optimization-actions/{action.id}/apply"
    assert (await client.post(url, json={})).status_code == 401
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    assert (await client.post(url, json={"human_comment": "x" * 2001})).status_code == 422
    result = await client.post(url, json={"human_comment": "Human"})
    assert result.status_code == 200
    payload = result.json()
    assert payload["action"]["status"] == "APPLIED"
    assert (await client.post(url, json={})).json()["artifact_id"] == payload["artifact_id"]
    reloaded = (await client.get(f"/api/v1/optimization-proposals/{action.proposal_id}")).json()
    assert reloaded["actions"][0]["applied_artifact"]["artifact_id"] == payload["artifact_id"]


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
async def test_existing_publication_immutable(db_session, kind):
    from app.models.publication import Publication, PublicationStatus

    user, campaign, item, version, *_, action = await setup_action(db_session, kind)
    row = Publication(
        campaign_id=campaign.id,
        content_item_id=item.id,
        content_version_id=version.id,
        channel=ContentChannel.TELEGRAM,
        status=PublicationStatus.SCHEDULED,
        scheduled_at=datetime(2026, 10, 10, tzinfo=UTC),
        approved_for_publish_by=user.id,
        approved_for_publish_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()
    original = await db_session.scalar(
        text("SELECT md5(row_to_json(t)::text) FROM publications t WHERE id=:id"), {"id": row.id}
    )
    await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Human")
    )
    assert original == await db_session.scalar(
        text("SELECT md5(row_to_json(t)::text) FROM publications t WHERE id=:id"), {"id": row.id}
    )
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 1


async def test_strategy_generation_requires_normal_human_approval(db_session):
    from app.tests.test_campaign_planning import plan_data

    user, campaign, *_, action = await setup_action(db_session, "STRATEGY_REVIEW")
    original = deepcopy(campaign.strategy)
    result = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest()
    )
    service = AgentRunService(db_session)
    assert await service.claim(result.agent_run_id)
    await service.finish_success(
        result.agent_run_id, RuntimeResult(plan_data(), 1, 10, 5, 15, None)
    )
    await db_session.refresh(campaign)
    assert campaign.strategy == original and campaign.strategy_version == 7
    assert campaign.status is CampaignStatus.WAITING_APPROVAL
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == campaign.id, Approval.status == ApprovalStatus.PENDING
        )
    )
    assert approval and approval.subject_version == 8 and approval.subject_snapshot == plan_data()
    await CampaignPlanningService(db_session).reject(campaign.id, user, "Human rejected new draft")
    assert (
        campaign.status is CampaignStatus.ACTIVE
        and campaign.strategy == original
        and campaign.strategy_version == 7
    )


@pytest.mark.parametrize(
    "remaining,expected",
    [
        ("PROPOSED", "PARTIALLY_APPROVED"),
        ("APPROVED", "PARTIALLY_APPROVED"),
        ("EXPERIMENT", "PARTIALLY_APPROVED"),
        ("NO_CHANGE", "APPLIED"),
        ("REJECTED", "APPLIED"),
    ],
)
async def test_aggregation_does_not_hide_unapplied_actions(db_session, remaining, expected):
    user, _, _, _, _, _, proposal, action = await setup_action(db_session, "STRATEGY_REVIEW")
    sibling = CampaignOptimizationAction(
        proposal_id=proposal.id,
        position=1,
        source_recommendation_index=1,
        type=AT.EXPERIMENT
        if remaining == "EXPERIMENT"
        else AT.NO_CHANGE
        if remaining == "NO_CHANGE"
        else AT.CONTENT_REVISION,
        target_entity_type=action.target_entity_type,
        target_entity_id=action.target_entity_id,
        reason="Reason",
        expected_effect="Effect",
        priority="LOW",
        evidence_refs=[],
        status=AS.APPROVED
        if remaining in {"APPROVED", "EXPERIMENT", "NO_CHANGE"}
        else AS(remaining),
    )
    db_session.add(sibling)
    await db_session.commit()
    await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest()
    )
    await db_session.refresh(proposal)
    await db_session.refresh(sibling)
    assert proposal.status.value == expected
    assert sibling.status is not AS.APPLIED


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
async def test_applied_requires_durable_artifact_flush(db_session, kind):
    from sqlalchemy import event

    user, *_, action = await setup_action(db_session, kind)
    observed = []

    def before_flush(session, ctx, instances):
        if action.status is AS.APPLIED:
            table = "publication_plans" if kind == "PUBLICATION_PLAN_REVISION" else "tasks"
            count = session.scalar(
                text(f"SELECT count(*) FROM {table} WHERE optimization_action_id=:id"),
                {"id": action.id},
            )
            assert count == 1
            observed.append(count)

    event.listen(db_session.sync_session, "before_flush", before_flush)
    try:
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment="Human")
        )
    finally:
        event.remove(db_session.sync_session, "before_flush", before_flush)
    assert observed


async def test_content_revision_normal_execution_new_version_separate_approval(db_session):
    from app.models.content import ContentStatus, ContentVersion
    from app.tests.test_automatic_workflow import _article_revision_output

    user, campaign, item, version, plan, _, _, action = await setup_action(db_session)
    old_hash = await db_session.scalar(
        text("SELECT md5(row_to_json(t)::text) FROM content_versions t WHERE id=:id"),
        {"id": version.id},
    )
    result = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Уточнить формулировки")
    )
    source = await db_session.scalar(
        select(ContentVersionSource).where(ContentVersionSource.content_version_id == version.id)
    )
    service = AgentRunService(db_session)
    assert await service.claim(result.agent_run_id)
    pack_item = await db_session.get(KnowledgePackItem, source.knowledge_pack_item_id)
    db_session.add(
        ToolCall(
            agent_run_id=result.agent_run_id,
            tool_name="read_knowledge_pack",
            arguments={"knowledge_pack_id": str(pack_item.knowledge_pack_id)},
            result={
                "pack_id": str(pack_item.knowledge_pack_id),
                "items": [{"knowledge_pack_item_id": str(pack_item.id)}],
            },
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    await service.finish_success(
        result.agent_run_id,
        RuntimeResult(
            _article_revision_output(str(source.knowledge_pack_item_id)), 1, 10, 5, 15, None
        ),
    )
    await db_session.refresh(item)
    assert item.current_version_id != version.id and item.status is ContentStatus.APPROVED
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentVersion)
            .where(ContentVersion.content_item_id == item.id)
        )
        == 2
    )
    assert old_hash == await db_session.scalar(
        text("SELECT md5(row_to_json(t)::text) FROM content_versions t WHERE id=:id"),
        {"id": version.id},
    )
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_id == item.id, Approval.status == ApprovalStatus.PENDING
        )
    )
    assert approval and approval.subject_snapshot["content_version_id"] == str(
        item.current_version_id
    )
    plan_item = await db_session.scalar(
        select(PublicationPlanItem).where(PublicationPlanItem.publication_plan_id == plan.id)
    )
    assert plan_item.source_content_version_id == version.id


class SimulatedProcessDeath(BaseException):
    """Bypass application exception handling as an abruptly killed API would."""


async def stranded_apply(db_session, monkeypatch, kind, *, accepted=False):
    user, _, _, version, *_, action = await setup_action(db_session, kind)
    if accepted and kind == "PUBLICATION_PLAN_REVISION":
        version.content = (
            "Команда выбирает приоритеты, но ежедневная операционная работа вытесняет "
            "их из управления. Владелец исполнения и регулярный обзор решений "
            "помогают вернуть выбранные приоритеты в рабочий контур."
        )
        await db_session.commit()
    worker = generate_publication_plan if kind == "PUBLICATION_PLAN_REVISION" else execute_agent_run
    deliveries = []

    def die(**kwargs):
        if accepted:
            deliveries.append(kwargs["args"][0])
        raise SimulatedProcessDeath

    monkeypatch.setattr(worker, "apply_async", die)
    with pytest.raises(SimulatedProcessDeath):
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment="Human")
        )
    # Drop the uncommitted enqueue transaction, preserving the Apply commit.
    await db_session.rollback()
    await db_session.refresh(action)
    await db_session.refresh(user)
    assert action.status is AS.APPLIED
    artifact = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest()
    )
    run = await db_session.get(AgentRun, artifact.agent_run_id)
    assert run.status is AgentRunStatus.QUEUED and run.queue_job_id is None
    assert (
        await db_session.scalar(
            select(func.count()).where(
                Task.campaign_id == run.campaign_id,
                Task.task_type
                == (
                    TaskType.MANUAL
                    if kind == "PUBLICATION_PLAN_REVISION"
                    else TaskType.CAMPAIGN_PLANNING
                ),
            )
        )
        == 1
    )

    def enqueue(**kwargs):
        deliveries.append(kwargs["args"][0])
        return SimpleNamespace(id="recovered-job")

    monkeypatch.setattr(worker, "apply_async", enqueue)
    return user, action, artifact, run, deliveries


@pytest.mark.parametrize("kind", ["PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"])
async def test_durable_apply_crash_recovery_same_run(db_session, monkeypatch, kind):
    from app.services.task_recovery_service import TaskRecoveryService

    user, action, artifact, run, deliveries = await stranded_apply(db_session, monkeypatch, kind)
    assert [r.id for r in await TaskRecoveryService(db_session).recover_stuck()] == [run.id]
    assert run.queue_job_id == "recovered-job"
    assert await TaskRecoveryService(db_session).recover_stuck() == []
    repeated = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest()
    )
    assert (repeated.artifact_id, repeated.task_id, repeated.agent_run_id) == (
        artifact.artifact_id,
        artifact.task_id,
        run.id,
    )
    assert deliveries == [str(run.id)]
    assert await db_session.scalar(select(func.count()).where(AgentRun.task_id == run.task_id)) == 1
    assert await count_artifacts(db_session, action.id) == (
        [0, 1] if kind == "PUBLICATION_PLAN_REVISION" else [1, 0]
    )


@pytest.mark.parametrize("kind", ["PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"])
async def test_concurrent_recovery_apply_and_normal_enqueue(db_session, monkeypatch, kind):
    from app.services.task_recovery_service import TaskRecoveryService

    user, action, artifact, run, deliveries = await stranded_apply(db_session, monkeypatch, kind)
    uid, aid, rid = user.id, action.id, run.id
    await db_session.commit()

    async def recover():
        async with async_session_factory() as session:
            return await TaskRecoveryService(session).recover_stuck()

    async def repeat():
        async with async_session_factory() as session:
            current_user = await session.get(User, uid)
            return await OptimizationProposalService(session).apply_action(
                aid, current_user, OptimizationActionApplyRequest()
            )

    async def normal_enqueue():
        async with async_session_factory() as session:
            current = await session.get(AgentRun, rid)
            if kind == "PUBLICATION_PLAN_REVISION":
                plan = await session.get(PublicationPlan, artifact.artifact_id)
                await PublicationPlanService(session).enqueue_generation(plan, current)
            else:
                await AgentRunService(session).enqueue(current)

    _, _, repeated, _ = await asyncio.gather(recover(), recover(), repeat(), normal_enqueue())
    assert repeated.artifact_id == artifact.artifact_id and repeated.agent_run_id == rid
    assert deliveries == [str(rid)]
    assert await recover() == []
    assert await count_artifacts(db_session, aid) == (
        [0, 1] if kind == "PUBLICATION_PLAN_REVISION" else [1, 0]
    )


@pytest.mark.parametrize("kind", ["PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"])
async def test_recovery_enqueue_failure_uses_existing_artifact(db_session, monkeypatch, kind):
    from app.services.task_recovery_service import TaskRecoveryService

    _, action, artifact, run, _ = await stranded_apply(db_session, monkeypatch, kind)
    worker = generate_publication_plan if kind == "PUBLICATION_PLAN_REVISION" else execute_agent_run

    def fail(**kwargs):
        raise RuntimeError("broker offline")

    monkeypatch.setattr(worker, "apply_async", fail)
    assert [r.id for r in await TaskRecoveryService(db_session).recover_stuck()] == [run.id]
    assert run.status is AgentRunStatus.FAILED
    task = await db_session.get(Task, artifact.task_id)
    assert task.status is TaskStatus.FAILED
    assert await TaskRecoveryService(db_session).recover_stuck() == []
    assert await count_artifacts(db_session, action.id) == (
        [0, 1] if kind == "PUBLICATION_PLAN_REVISION" else [1, 0]
    )


@pytest.mark.parametrize("kind", ["PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"])
async def test_ambiguous_delivery_claims_same_run_once(db_session, monkeypatch, kind):
    from app.services.task_recovery_service import TaskRecoveryService
    from app.workers import publication_plan_worker

    _, _, artifact, run, deliveries = await stranded_apply(
        db_session, monkeypatch, kind, accepted=True
    )
    await TaskRecoveryService(db_session).recover_stuck()
    assert deliveries == [str(run.id), str(run.id)]
    if kind == "STRATEGY_REVIEW":

        async def claim():
            async with async_session_factory() as session:
                return await AgentRunService(session).claim(run.id)

        first, second = await asyncio.gather(claim(), claim())
        assert sum(result is not None for result in (first, second)) == 1
        from app.tests.test_campaign_planning import plan_data

        await db_session.refresh(run)
        service = AgentRunService(db_session)
        result = RuntimeResult(plan_data(), 1, 10, 5, 15, None)
        await service.finish_success(run.id, result)
        await service.finish_success(run.id, result)
        assert (
            await db_session.scalar(
                select(func.count()).where(Approval.object_id == run.campaign_id)
            )
            == 1
        )
    else:
        # The winner deliberately pauses after the committed claim. The duplicate
        # must return before constructing a provider client or writing plan items.
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        from uuid import UUID

        from app.tests.test_publication_plan import _planner_result

        output = _planner_result(
            UUID(run.input_data["publication_plan_snapshot"]["article_version_ids"][0])
        )
        output.items[0].source_claim_ids = [
            run.input_data["publication_plan_snapshot"]["article_digests"][0]["allowed_claims"][0][
                "claim_id"
            ]
        ]

        class PausedClient:
            def __init__(self, **kwargs):
                calls.append("client")

            async def close(self):
                pass

        async def paused_runner(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(final_output=output)

        monkeypatch.setattr(settings, "openai_api_key", "offline-placeholder")
        monkeypatch.setattr(publication_plan_worker, "AsyncOpenAI", PausedClient)
        monkeypatch.setattr(publication_plan_worker.Runner, "run", paused_runner)
        # Responses model construction stays offline and accepts the fake client.
        monkeypatch.setattr(publication_plan_worker, "OpenAIResponsesModel", lambda *a: None)
        winner = asyncio.create_task(publication_plan_worker._run(run.id))
        await asyncio.wait_for(entered.wait(), timeout=10)
        await publication_plan_worker._run(run.id)
        assert calls == ["client"]
        release.set()
        await winner
        await publication_plan_worker._run(run.id)
        assert (
            await db_session.scalar(
                select(func.count()).where(
                    PublicationPlanItem.publication_plan_id == artifact.artifact_id
                )
            )
            == 1
        )
        await db_session.refresh(run)
        assert run.status is AgentRunStatus.COMPLETED
        assert calls == ["client"]
    assert await db_session.scalar(select(func.count()).where(AgentRun.task_id == run.task_id)) == 1


async def test_content_apply_commit_crash_keeps_autonomous_dispatch(db_session, monkeypatch):
    from app.services.task_dispatcher_service import TaskDispatcherService
    from app.services.task_recovery_service import TaskRecoveryService

    user, *_, action = await setup_action(db_session)
    original = AgentRunService.create_queued_run

    async def die(*args, **kwargs):
        raise SimulatedProcessDeath

    monkeypatch.setattr(AgentRunService, "create_queued_run", die)
    with pytest.raises(SimulatedProcessDeath):
        await OptimizationProposalService(db_session).apply_action(
            action.id, user, OptimizationActionApplyRequest(human_comment="Human")
        )
    await db_session.rollback()
    await db_session.refresh(action)
    task = await db_session.scalar(select(Task).where(Task.optimization_action_id == action.id))
    assert action.status is AS.APPLIED and task.status is TaskStatus.READY
    assert await db_session.scalar(select(func.count()).where(AgentRun.task_id == task.id)) == 0
    assert await TaskRecoveryService(db_session).recover_stuck() == []
    monkeypatch.setattr(AgentRunService, "create_queued_run", original)
    assert task.id in await TaskDispatcherService(db_session).dispatch_ready_tasks()
    assert await db_session.scalar(select(func.count()).where(AgentRun.task_id == task.id)) == 1


@pytest.mark.parametrize("kind", ["PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"])
async def test_recovery_requires_applied_durable_action(db_session, monkeypatch, kind):
    from app.services.task_recovery_service import TaskRecoveryService

    _, action, _, _, deliveries = await stranded_apply(db_session, monkeypatch, kind)
    action.status = AS.APPROVED
    await db_session.commit()
    assert await TaskRecoveryService(db_session).recover_stuck() == []
    assert deliveries == []
