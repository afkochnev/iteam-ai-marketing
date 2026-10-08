"""PR32 persisted read models and closed-loop acceptance. All execution is offline."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from app.api.content import approve_content
from app.core.config import settings
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_kpi import CampaignKPI, KPIComparison, KPIMetric
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.marketing_experiment import MarketingExperiment
from app.models.marketing_feedback import (
    AnalysisTriggerSource,
    FeedbackAnalysisStatus,
    MarketingFeedbackAnalysis,
)
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionStatus,
    OptimizationActionType,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.models.publication_plan import PublicationPlan, PublicationPlanItem, PublicationPlanStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.tasks import TaskRepository
from app.schemas.content import ContentApprovalRequest
from app.schemas.optimization import OptimizationActionApplyRequest
from app.schemas.publication import PublicationCreate
from app.schemas.task import task_to_response
from app.services.approval_service import ApprovalService
from app.services.campaign_workspace_service import CampaignWorkspaceService
from app.services.feedback_service import FeedbackService
from app.services.optimization_proposal_service import OptimizationProposalService
from app.services.optimization_provenance_service import OptimizationProvenanceService
from app.services.optimization_workspace_service import OptimizationWorkspaceService
from app.services.performance_analysis_discovery_service import PerformanceAnalysisDiscoveryService
from app.services.publication_plan_service import PublicationPlanService
from app.services.publication_service import PublicationService
from app.services.task_classification_service import classify_tasks
from app.tests.test_campaign_workspace import workspace_fixture
from app.tests.test_optimization import draft, recommendation
from app.tests.test_optimization_apply import setup_action
from app.tests.test_performance_analysis import dispatch, fixture, mock_model, result
from app.tests.test_publication_plan import _planner_result
from app.workers import agent_worker, feedback_worker, publication_plan_worker


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for task in [
        agent_worker.execute_agent_run,
        feedback_worker.generate_feedback_analysis,
        publication_plan_worker.generate_publication_plan,
    ]:
        monkeypatch.setattr(
            task, "apply_async", Mock(return_value=SimpleNamespace(id="offline-queue"))
        )


async def counts(session):
    return [
        await session.scalar(select(func.count()).select_from(m))
        for m in [
            Task,
            AgentRun,
            Publication,
            MarketingFeedbackAnalysis,
            ContentVersion,
            PublicationPlan,
            PublicationPlanItem,
            MarketingExperiment,
            CampaignOptimizationProposal,
            CampaignOptimizationAction,
            ActivityLog,
        ]
    ]


async def test_legacy_failed_task_classification_and_read_only_detail(db_session):
    data = await workspace_fixture(db_session, with_failure=True)
    task = data["failure_task"]
    task.input_data = {"feedback_analysis": True}
    await db_session.commit()
    classified = await classify_tasks(db_session, [task])
    assert classified[task.id] == ("historical", "Исторический анализ обратной связи")
    response = task_to_response(await TaskRepository(db_session).get_by_id(task.id))
    assert response.historical_feedback_analysis is True
    workspace = await CampaignWorkspaceService(db_session).get(data["campaign"].id)
    assert workspace.director.failed_task_count == 0
    assert workspace.director.next_step.entity_id != task.id
    assert task.status == TaskStatus.FAILED


@pytest.mark.parametrize("durable", [False, True])
async def test_legacy_draft_does_not_block_but_current_draft_does(db_session, durable):
    user, campaign, *_ = await fixture(db_session)
    service = FeedbackService(db_session)
    if durable:
        row = await service.prepare_analysis(campaign.id)
    else:
        row = MarketingFeedbackAnalysis(
            campaign_id=campaign.id,
            strategy_version=campaign.strategy_version,
            status=FeedbackAnalysisStatus.DRAFT,
            input_snapshot={},
            findings=[],
            recommendations=[],
            experiment_ideas=[],
            limitations=[],
            created_at=datetime.now(UTC) - timedelta(days=3),
        )
        db_session.add(row)
        await db_session.commit()
    original = row.status
    created = await PerformanceAnalysisDiscoveryService(db_session).discover()
    assert len(created) == (0 if durable else 1)
    await db_session.refresh(row)
    assert row.status == original
    assert await PerformanceAnalysisDiscoveryService(db_session).discover() == []


async def test_historical_draft_superseded_by_reviewed_analysis(db_session):
    user, campaign, *_ = await fixture(db_session)
    old = MarketingFeedbackAnalysis(
        campaign_id=campaign.id,
        strategy_version=1,
        status=FeedbackAnalysisStatus.DRAFT,
        input_snapshot={},
        findings=[],
        recommendations=[],
        experiment_ideas=[],
        limitations=[],
        generated_at=datetime.now(UTC) - timedelta(days=2),
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    reviewed = MarketingFeedbackAnalysis(
        campaign_id=campaign.id,
        strategy_version=1,
        status=FeedbackAnalysisStatus.REJECTED,
        input_snapshot={},
        findings=[],
        recommendations=[],
        experiment_ideas=[],
        limitations=[],
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add_all([old, reviewed])
    await db_session.commit()
    state = (await OptimizationWorkspaceService(db_session).states([campaign.id]))[campaign.id]
    assert old.id in state.superseded_legacy_analysis_ids and state.review_analyses == []
    assert old.status == FeedbackAnalysisStatus.DRAFT


@pytest.mark.parametrize(
    "state,expected",
    [
        ("failed", "Разобрать ошибку задачи"),
        ("approval", "Согласовать пост"),
        ("analysis", "Проверить анализ результатов"),
        ("decision", "Рассмотреть рекомендации"),
        ("apply", "Применить принятую рекомендацию"),
        ("processing", "Идёт анализ новых результатов"),
        ("new", "Получены новые результаты"),
        ("normal", "Кампания работает штатно"),
    ],
)
async def test_next_step_priority_matrix(db_session, state, expected):
    data = await workspace_fixture(db_session, with_post=True, with_failure=state == "failed")
    campaign = data["campaign"]
    user = await db_session.scalar(select(User))
    pub = data["publication"]
    pub.status = PublicationStatus.PUBLISHED
    pub.published_at = datetime.now(UTC)
    # Existing workflow is complete; pending optimization competes with normal monitoring.
    row = MarketingFeedbackAnalysis(
        campaign_id=campaign.id,
        strategy_version=campaign.strategy_version,
        status=FeedbackAnalysisStatus.ACCEPTED,
        input_snapshot={},
        findings=[],
        recommendations=[],
        experiment_ideas=[],
        limitations=[],
    )
    db_session.add(row)
    await db_session.flush()
    proposal = CampaignOptimizationProposal(
        campaign_id=campaign.id,
        feedback_analysis_id=row.id,
        strategy_version=campaign.strategy_version,
        status="WAITING_APPROVAL",
        summary="Recommendation",
    )
    db_session.add(proposal)
    await db_session.flush()
    action = CampaignOptimizationAction(
        proposal_id=proposal.id,
        position=0,
        type=OptimizationActionType.CONTENT_REVISION,
        target_entity_type="CONTENT_ITEM",
        target_entity_id=data["post"].id,
        target_version_id=data["post_version"].id,
        reason="Specific evidence",
        expected_effect="Clarity",
        priority="HIGH",
        evidence_refs=[],
        status=OptimizationActionStatus.PROPOSED
        if state in ["failed", "approval", "decision"]
        else OptimizationActionStatus.APPROVED
        if state == "apply"
        else OptimizationActionStatus.REJECTED,
        source_recommendation_index=0,
    )
    db_session.add(action)
    if state == "approval":
        data["post"].status = ContentStatus.WAITING_APPROVAL
    if state in ["analysis", "processing"]:
        task = Task(
            campaign_id=campaign.id,
            task_type=TaskType.ANALYZE_PERFORMANCE,
            title="Analysis",
            status=TaskStatus.COMPLETED if state == "analysis" else TaskStatus.READY,
        )
        db_session.add(task)
        await db_session.flush()
        row.status = FeedbackAnalysisStatus.DRAFT
        row.task_id = task.id
        row.evidence_fingerprint = "a" * 64
        row.generated_at = datetime.now(UTC) if state == "analysis" else None
    if state == "new":
        await FeedbackService(db_session).create_feedback(
            campaign.id, user, {"category": "TONE", "comment": "Test fixture evidence"}
        )
    await db_session.commit()
    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)
    assert workspace.director.next_step.title == expected
    assert f"/campaigns/{campaign.id}#" not in workspace.director.next_step.href


@pytest.mark.parametrize(
    "kind", ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW"]
)
async def test_exact_downstream_provenance_and_read_models_side_effect_free(db_session, kind):
    user, campaign, item, version, old_plan, analysis, proposal, action = await setup_action(
        db_session, kind
    )
    artifact = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Reviewed individually")
    )
    before = await counts(db_session)
    service = OptimizationProvenanceService(db_session)
    response = await service.get(action.id, campaign.id)
    assert artifact.artifact_id in {x.id for x in response.downstream_artifacts}
    assert response.analysis.id == analysis.id and response.proposal.id == proposal.id
    assert await counts(db_session) == before
    await OptimizationWorkspaceService(db_session).dashboard()
    await CampaignWorkspaceService(db_session).get(campaign.id)
    assert await counts(db_session) == before
    if kind == "STRATEGY_REVIEW":
        assert any("нет прямого ключа" in gap for gap in response.limitations)
    if kind == "CONTENT_REVISION":
        assert any(
            v.id == version.id and v.details["source_target"] for v in response.content_versions
        )
        assert response.publications == []  # Unpublished revision is never presented as delivered.
    with pytest.raises(AppError) as error:
        await service.get(action.id, uuid4())
    assert error.value.status_code == 404


async def test_evidence_allowlist_cross_campaign_and_null_vs_zero(db_session):
    user, campaign, item, version, plan, analysis, proposal, action = await setup_action(
        db_session, "NO_CHANGE"
    )
    pub = Publication(
        campaign_id=campaign.id,
        content_item_id=item.id,
        content_version_id=version.id,
        channel=ContentChannel.TELEGRAM,
        status=PublicationStatus.PUBLISHED,
        published_at=datetime.now(UTC),
    )
    db_session.add(pub)
    await db_session.flush()
    metric = PublicationMetricsSnapshot(
        publication_id=pub.id,
        channel=pub.channel,
        observed_at=datetime.now(UTC),
        source=MetricsSource.MANUAL,
        views=0,
        clicks=None,
    )
    db_session.add(metric)
    await db_session.flush()
    analysis.input_snapshot = {**analysis.input_snapshot, "metrics_snapshot_ids": [str(metric.id)]}
    action.evidence_refs = [
        {"type": "metrics_snapshot", "id": str(metric.id)},
        {"type": "publication", "id": str(pub.id)},
    ]
    await db_session.commit()
    response = await OptimizationProvenanceService(db_session).get(action.id)
    assert len(response.resolved_evidence) == 1
    assert response.resolved_evidence[0].details["metrics"]["views"] == 0
    assert response.resolved_evidence[0].details["metrics"]["clicks"] is None
    assert response.resolved_evidence[0].details["content_version_id"] == str(version.id)
    assert any("не разрешена" in x for x in response.limitations)


async def test_full_mvp3_mocked_plan_revision_e2e(db_session, monkeypatch):
    user, campaign, article, source_version, old_plan, old_analysis, _, _ = await setup_action(
        db_session, "NO_CHANGE"
    )
    # Complete the isolated source fixture before freezing the E2E baseline.
    source_version.content = (
        "Команда выбирает приоритеты, но ежедневная операционная работа вытесняет "
        "их из управления. Владелец исполнения и регулярный обзор решений "
        "помогают вернуть выбранные приоритеты в рабочий контур."
    )
    old_analysis.created_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.execute(
        update(Task).where(Task.status == TaskStatus.READY).values(status=TaskStatus.COMPLETED)
    )
    analyst = Agent(
        name="Marketing Analyst",
        slug="marketing_analyst",
        role="marketing_analyst",
        status=AgentStatus.ACTIVE,
        system_prompt=Path("app/prompts/marketing_analyst.md").read_text(),
        model="test-model",
        autonomy_level=1,
        settings={},
    )
    pub = Publication(
        campaign_id=campaign.id,
        content_item_id=article.id,
        content_version_id=source_version.id,
        channel=ContentChannel.TELEGRAM,
        status=PublicationStatus.PUBLISHED,
        published_at=datetime.now(UTC),
    )
    db_session.add_all([analyst, pub])
    await db_session.flush()
    metric = PublicationMetricsSnapshot(
        publication_id=pub.id,
        channel=pub.channel,
        observed_at=datetime.now(UTC),
        source=MetricsSource.MANUAL,
        views=0,
        clicks=None,
    )
    kpi = CampaignKPI(
        campaign_id=campaign.id,
        metric=KPIMetric.VIEWS,
        target_value=10,
        comparison=KPIComparison.GTE,
        period_start=datetime.now(UTC) - timedelta(days=1),
        period_end=datetime.now(UTC) + timedelta(days=1),
        is_active=True,
    )
    db_session.add_all([metric, kpi])
    await db_session.commit()
    await FeedbackService(db_session).create_feedback(
        campaign.id, user, {"category": "TONE", "comment": "Offline acceptance fixture"}
    )
    before_old = (
        old_plan.status,
        deepcopy(old_plan.items[0].message_brief),
        source_version.content,
    )
    ids = await PerformanceAnalysisDiscoveryService(db_session).discover()
    assert len(ids) == 1
    row = await db_session.get(MarketingFeedbackAnalysis, ids[0])
    assert row.trigger_source == AnalysisTriggerSource.AUTOMATIC
    run = await dispatch(db_session, row, monkeypatch)

    async def analyst_result(*args, **kwargs):
        output = result(row)
        output["findings"] = [
            {
                "type": "METRICS",
                "observation": "Measured view count",
                "evidence_refs": [{"type": "metrics_snapshot", "id": str(metric.id)}],
                "confidence": "provided",
            }
        ]
        action = draft(
            row.input_snapshot,
            "PUBLICATION_PLAN_REVISION",
            target_entity_type="PUBLICATION_PLAN",
            target_entity_id=str(old_plan.id),
            evidence_refs=[{"type": "metrics_snapshot", "id": str(metric.id)}],
        )
        output["recommendations"] = [recommendation(action)]
        return Mock(final_output=output)

    proposals_before = await db_session.scalar(
        select(func.count()).select_from(CampaignOptimizationProposal)
    )
    mock_model(monkeypatch, analyst_result)
    await feedback_worker._generate(run.id)
    await db_session.refresh(row)
    await db_session.refresh(run)
    assert row.status == FeedbackAnalysisStatus.DRAFT and run.status == AgentRunStatus.COMPLETED
    task = await db_session.get(Task, row.task_id)
    assert task.status == TaskStatus.COMPLETED
    assert run.input_data["executable_tools"] == []
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal))
        == proposals_before
    )
    await FeedbackService(db_session).review(row.id, user, FeedbackAnalysisStatus.ACCEPTED)
    proposal = await db_session.scalar(
        select(CampaignOptimizationProposal).where(
            CampaignOptimizationProposal.feedback_analysis_id == row.id
        )
    )
    action = proposal.actions[0]
    await OptimizationProposalService(db_session).decide(
        action.id, user, OptimizationActionStatus.APPROVED
    )
    artifact = await OptimizationProposalService(db_session).apply_action(
        action.id, user, OptimizationActionApplyRequest(human_comment="Human review")
    )
    new_plan = await PublicationPlanService(db_session).get_plan(artifact.artifact_id)
    assert new_plan.status == PublicationPlanStatus.DRAFT and new_plan.id != old_plan.id
    plan_run = await db_session.get(AgentRun, artifact.agent_run_id)
    output = _planner_result(
        UUID(plan_run.input_data["publication_plan_snapshot"]["article_version_ids"][0])
    )
    output.items[0].scheduled_at = old_plan.planning_horizon_start + timedelta(days=4)
    output.items[0].source_claim_ids = [
        plan_run.input_data["publication_plan_snapshot"]["article_digests"][0]["allowed_claims"][0][
            "claim_id"
        ]
    ]

    class Client:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            pass

    async def plan_result(*args, **kwargs):
        return SimpleNamespace(final_output=output)

    monkeypatch.setattr(publication_plan_worker, "AsyncOpenAI", Client)
    monkeypatch.setattr(publication_plan_worker, "OpenAIResponsesModel", lambda *args: None)
    monkeypatch.setattr(publication_plan_worker.Runner, "run", plan_result)
    await publication_plan_worker._run(plan_run.id)
    new_plan = await PublicationPlanService(db_session).get_plan(new_plan.id)
    await PublicationPlanService(db_session).transition(
        new_plan.id, user, PublicationPlanStatus.WAITING_APPROVAL
    )
    new_plan = await PublicationPlanService(db_session).transition(
        new_plan.id, user, PublicationPlanStatus.APPROVED
    )
    smm = await db_session.scalar(select(Agent).where(Agent.slug == "smm_manager"))
    downstream_task = Task(
        campaign_id=campaign.id,
        task_type=TaskType.CREATE_SOCIAL_POSTS,
        title="Normal post fixture",
        assigned_agent_id=smm.id,
        status=TaskStatus.COMPLETED,
        input_data={"publication_plan_item_id": str(new_plan.items[0].id)},
    )
    db_session.add(downstream_task)
    await db_session.flush()
    post = ContentItem(
        campaign_id=campaign.id,
        source_task_id=downstream_task.id,
        content_type=ContentType.SOCIAL_POST,
        title="Revised plan post",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=smm.id,
        channel=ContentChannel.TELEGRAM,
        metadata_={
            "publication_plan_item_id": str(new_plan.items[0].id),
            "publication_plan_id": str(new_plan.id),
            "source_content_version_id": str(source_version.id),
        },
    )
    db_session.add(post)
    await db_session.flush()
    version = ContentVersion(
        content_item_id=post.id,
        version_number=1,
        content="Offline post fixture",
        structured_content={},
    )
    db_session.add(version)
    await db_session.flush()
    post.current_version_id = version.id
    await ApprovalService(db_session).create_content_approval(
        post.id, 1, {"content_version_id": str(version.id)}, smm.id
    )
    await db_session.commit()
    await approve_content(post.id, ContentApprovalRequest(), user, db_session)
    monkeypatch.setattr(
        "app.services.publication_service.utc_now",
        lambda: old_plan.planning_horizon_start,
    )
    final_pub = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    assert final_pub.content_version_id == version.id
    provenance = await OptimizationProvenanceService(db_session).get(action.id)
    assert metric.id in {e.id for e in provenance.resolved_evidence}
    assert provenance.analysis.id == row.id and provenance.proposal.id == proposal.id
    assert new_plan.id in {n.id for n in provenance.downstream_artifacts}
    assert version.id in {n.id for n in provenance.content_versions}
    assert final_pub.id in {n.id for n in provenance.publications}
    assert next(n for n in provenance.publications if n.id == final_pub.id).details[
        "content_version_id"
    ] == str(version.id)
    old_plan = await PublicationPlanService(db_session).get_plan(old_plan.id)
    await db_session.refresh(source_version)
    assert before_old == (old_plan.status, old_plan.items[0].message_brief, source_version.content)
    assert await PerformanceAnalysisDiscoveryService(db_session).discover() == []


async def test_experiment_provenance_uses_only_explicit_publication_links(db_session):
    from app.tests.test_experiments import apply
    from app.tests.test_experiments import fixture as experiment_fixture

    user, campaign, analysis, proposal, action, publications, config = await experiment_fixture(
        db_session
    )
    artifact = await apply(db_session, user, action, config)
    before = await counts(db_session)
    response = await OptimizationProvenanceService(db_session).get(action.id)
    assert artifact.artifact_id in {node.id for node in response.downstream_artifacts}
    assert {node.id for node in response.publications} == {p.id for p in publications}
    assert {node.id for node in response.content_versions} >= {
        p.content_version_id for p in publications
    }
    assert await counts(db_session) == before


async def test_dashboard_and_provenance_available_to_manager_without_mutation(client, db_session):
    from app.models.user import UserRole
    from app.services.auth_service import AuthService

    user, campaign, item, version, plan, analysis, proposal, action = await setup_action(
        db_session, "CONTENT_REVISION"
    )
    user.role = UserRole.MANAGER
    await db_session.commit()
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    before = await counts(db_session)
    dashboard = await client.get("/api/v1/dashboard/optimization")
    assert dashboard.status_code == 200
    assert dashboard.json()["counts"]["actions_waiting_apply"] == 1
    provenance = await client.get(f"/api/v1/optimization-actions/{action.id}/provenance")
    assert provenance.status_code == 200
    assert await counts(db_session) == before
    campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    dashboard = await client.get("/api/v1/dashboard/optimization")
    assert all(item["campaign_id"] != str(campaign.id) for item in dashboard.json()["items"])
    assert (
        await client.get(f"/api/v1/optimization-actions/{action.id}/provenance")
    ).status_code == 200


async def test_allowlisted_foreign_campaign_evidence_is_not_resolved(db_session):
    user, campaign, item, version, plan, analysis, proposal, action = await setup_action(
        db_session, "NO_CHANGE"
    )
    other = Campaign(
        name="Other campaign",
        goal="Isolated test",
        status=CampaignStatus.ACTIVE,
        created_by=user.id,
    )
    db_session.add(other)
    await db_session.flush()
    publication = Publication(
        campaign_id=other.id,
        content_item_id=item.id,
        content_version_id=version.id,
        channel=ContentChannel.TELEGRAM,
        status=PublicationStatus.PUBLISHED,
    )
    db_session.add(publication)
    await db_session.flush()
    metric = PublicationMetricsSnapshot(
        publication_id=publication.id,
        channel=publication.channel,
        observed_at=datetime.now(UTC),
        source=MetricsSource.MANUAL,
        views=12,
    )
    db_session.add(metric)
    await db_session.flush()
    analysis.input_snapshot = {
        **analysis.input_snapshot,
        "publication_metrics_snapshot_ids": [str(metric.id)],
    }
    action.evidence_refs = [{"type": "metrics_snapshot", "id": str(metric.id)}]
    await db_session.commit()
    response = await OptimizationProvenanceService(db_session).get(action.id)
    assert response.resolved_evidence == []
    assert any("другой кампании" in limitation for limitation in response.limitations)
