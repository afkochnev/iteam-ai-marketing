import asyncio
import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.core.config import Settings, settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.agent import Agent, AgentStatus, AgentTool
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import CampaignStatus
from app.models.marketing_feedback import (
    AnalysisTriggerSource,
    FeedbackAnalysisStatus,
    MarketingFeedbackAnalysis,
)
from app.models.optimization import CampaignOptimizationProposal
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.models.task import Task, TaskStatus, TaskType
from app.schemas.feedback import FeedbackAnalysisResponse, FeedbackAnalystResult
from app.schemas.publication import PublicationCreate
from app.schemas.task import TaskCreate, TaskUpdate
from app.services.agent_run_service import AgentRunService
from app.services.feedback_service import FeedbackService
from app.services.performance_analysis_discovery_service import PerformanceAnalysisDiscoveryService
from app.services.performance_evidence import canonical_json, evidence_fingerprint
from app.services.publication_service import PublicationService
from app.services.task_dispatcher_service import AUTO_TASK_TYPES, TaskDispatcherService
from app.services.task_recovery_service import TaskRecoveryService
from app.services.task_service import TaskService
from app.tests.test_publications import _approved_post
from app.workers import feedback_worker
from app.workers.scheduler_config import build_beat_schedule


async def fixture(session, *, evidence=True):
    user, campaign, post, version = await _approved_post(session)
    analyst = Agent(
        name="Marketing Analyst",
        slug="marketing_analyst",
        role="marketing_analyst",
        system_prompt=Path("app/prompts/marketing_analyst.md").read_text(),
        model="test-model",
        status=AgentStatus.ACTIVE,
        autonomy_level=1,
        settings={},
    )
    await session.execute(
        update(Task).where(Task.status == TaskStatus.READY).values(status=TaskStatus.COMPLETED)
    )
    session.add(analyst)
    await session.commit()
    if evidence:
        await FeedbackService(session).create_feedback(
            campaign.id, user, {"category": "TONE", "comment": "Human evidence"}
        )
    return user, campaign, post, version, analyst


async def append_feedback(session, campaign, user):
    return await FeedbackService(session).create_feedback(
        campaign.id, user, {"category": "TONE", "comment": "New evidence"}
    )


async def dispatch(session, row, monkeypatch):
    job = Mock(id="analysis-job")
    monkeypatch.setattr(
        feedback_worker.generate_feedback_analysis, "apply_async", Mock(return_value=job)
    )
    await TaskDispatcherService(session).dispatch_ready_tasks()
    await session.refresh(row)
    run = await session.get(AgentRun, row.agent_run_id)
    assert run is not None
    return run


def mock_model(monkeypatch, callback):
    class Client:
        async def close(self):
            pass

    monkeypatch.setattr(settings, "openai_api_key", "test-only-never-real")
    monkeypatch.setattr(feedback_worker, "AsyncOpenAI", lambda **kwargs: Client())
    monkeypatch.setattr(feedback_worker, "OpenAIResponsesModel", lambda **kwargs: object())
    monkeypatch.setattr(feedback_worker, "Runner", type("Runner", (), {"run": callback}))


def result(row, *, invalid=False):
    return {
        "summary": "Observed evidence",
        "findings": [
            {
                "type": "HUMAN",
                "observation": "Feedback provided",
                "evidence_refs": [
                    {
                        "type": "marketing_feedback",
                        "id": str(uuid4()) if invalid else row.input_snapshot["feedback_ids"][0],
                    }
                ],
                "confidence": "provided",
            }
        ],
        "interpretations": [
            {
                "interpretation": "One possible explanation",
                "supporting_findings": [0],
                "confidence": "low",
                "limitations": ["Not causal"],
            }
        ],
        "recommendations": [],
        "experiment_ideas": [],
        "limitations": ["Small sample"],
    }


@pytest.mark.parametrize(
    "change", ["none", "order", "time", "kpi", "strategy", "plan", "metric", "feedback"]
)
def test_canonical_identity_ignores_context_and_order(change):
    snapshot = {
        "campaign_id": str(uuid4()),
        "publication_metrics_snapshot_ids": ["b", "a"],
        "marketing_feedback_ids": ["d", "c"],
    }
    modified = copy.deepcopy(snapshot)
    if change == "order":
        modified["publication_metrics_snapshot_ids"].reverse()
        modified["marketing_feedback_ids"].reverse()
    elif change == "metric":
        modified["publication_metrics_snapshot_ids"].append("new")
    elif change == "feedback":
        modified["marketing_feedback_ids"].append("new")
    else:
        modified[change] = datetime.now(UTC).isoformat()
    fingerprint = evidence_fingerprint(snapshot)
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()
    assert (fingerprint != evidence_fingerprint(modified)) == (change in {"metric", "feedback"})


@pytest.mark.parametrize("position", [-1, 1, 7])
def test_interpretation_rejects_manufactured_finding(position):
    row = Mock(input_snapshot={"feedback_ids": [str(uuid4())]})
    payload = result(row)
    payload["interpretations"][0]["supporting_findings"] = [position]
    with pytest.raises(ValidationError):
        FeedbackAnalystResult.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["optimization_analysis_cooldown_hours", "optimization_analysis_scan_interval_seconds"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_analysis_configuration_positive(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


async def test_manual_durable_idempotent_no_run_and_frozen_context(db_session):
    user, campaign, _, version, agent = await fixture(db_session)
    service = FeedbackService(db_session)
    row = await service.prepare_analysis(campaign.id, requested_by_user_id=user.id)
    same = await service.prepare_analysis(campaign.id)
    assert row.id == same.id and row.task_id == same.task_id
    assert row.agent_run_id is None and row.trigger_source is AnalysisTriggerSource.MANUAL
    task = await db_session.get(Task, row.task_id)
    assert task.task_type is TaskType.ANALYZE_PERFORMANCE and task.status is TaskStatus.READY
    assert task.assigned_agent_id == agent.id and task.is_internal is False
    assert row.evidence_fingerprint == evidence_fingerprint(row.input_snapshot)
    assert row.input_snapshot["analysis_period_start"] < row.input_snapshot["analysis_period_end"]
    assert canonical_json(row.input_snapshot) == canonical_json(
        await service._input_snapshot(campaign.id)
    )
    assert await db_session.scalar(select(func.count()).select_from(MarketingFeedbackAnalysis)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0
    with pytest.raises(AppError) as error:
        await service.review(row.id, user, FeedbackAnalysisStatus.ACCEPTED)
    assert error.value.code == "ANALYSIS_NOT_GENERATED"


@pytest.mark.parametrize("automatic", [False, True])
async def test_archived_never_prepares(db_session, automatic):
    _, campaign, *_ = await fixture(db_session)
    campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).prepare_analysis(campaign.id, automatic=automatic)
    assert error.value.code == "CAMPAIGN_ARCHIVED"
    await db_session.rollback()
    assert await PerformanceAnalysisDiscoveryService(db_session).discover() == []


@pytest.mark.parametrize("inactive", [True, False])
async def test_missing_or_inactive_analyst_no_director_fallback(db_session, inactive):
    _, campaign, _, _, agent = await fixture(db_session)
    if inactive:
        agent.status = AgentStatus.INACTIVE
    else:
        await db_session.delete(agent)
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).prepare_analysis(campaign.id)
    assert error.value.code == "MARKETING_ANALYST_UNAVAILABLE"
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(MarketingFeedbackAnalysis)) == 0


async def test_auto_no_evidence_active_gate_cooldown_and_manual_bypass(db_session):
    user, campaign, *_ = await fixture(db_session, evidence=False)
    discovery = PerformanceAnalysisDiscoveryService(db_session)
    assert await discovery.discover() == []
    await append_feedback(db_session, campaign, user)
    ids = await discovery.discover()
    assert len(ids) == 1
    row = await db_session.get(MarketingFeedbackAnalysis, ids[0])
    assert row.trigger_source is AnalysisTriggerSource.AUTOMATIC
    assert await discovery.discover() == []
    await append_feedback(db_session, campaign, user)
    assert await discovery.discover() == []  # DRAFT
    row.status = FeedbackAnalysisStatus.REJECTED
    await db_session.commit()
    assert await discovery.discover() == []  # cooldown
    manual = await FeedbackService(db_session).prepare_analysis(campaign.id)
    assert manual.id != row.id and manual.trigger_source is AnalysisTriggerSource.MANUAL
    manual.status = FeedbackAnalysisStatus.REJECTED
    row.created_at = manual.created_at = datetime.now(UTC) - timedelta(hours=25)
    await db_session.commit()
    assert await discovery.discover() == []  # same fingerprint, regardless of context
    await append_feedback(db_session, campaign, user)
    assert len(await discovery.discover()) == 1


@pytest.mark.parametrize("race", ["manual-auto", "auto-auto", "manual-manual"])
async def test_concurrent_preparation_one_task_analysis(db_session, race):
    _, campaign, *_ = await fixture(db_session)
    campaign_id = campaign.id

    async def prepare(automatic):
        async with async_session_factory() as session:
            return await FeedbackService(session).prepare_analysis(
                campaign_id,
                AnalysisTriggerSource.AUTOMATIC if automatic else AnalysisTriggerSource.MANUAL,
                automatic=automatic,
            )

    first, second = await asyncio.gather(
        prepare(race != "manual-manual"), prepare(race == "auto-auto")
    )
    assert first.id == second.id
    assert await db_session.scalar(select(func.count()).select_from(MarketingFeedbackAnalysis)) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.task_type == TaskType.ANALYZE_PERFORMANCE)
        )
        == 1
    )


async def test_dispatch_frozen_prompt_model_tools_specialized_queue(db_session, monkeypatch):
    _, campaign, _, _, analyst = await fixture(db_session)
    analyst.model = None
    db_session.add(AgentTool(agent_id=analyst.id, tool_name="save_content"))
    await db_session.commit()
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    frozen = copy.deepcopy(row.input_snapshot)
    campaign.strategy_version += 1
    await db_session.commit()
    monkeypatch.setattr(settings, "openai_default_model", "analysis-fallback")
    run = await dispatch(db_session, row, monkeypatch)
    assert run.model == "analysis-fallback" and run.agent_id == analyst.id
    assert run.prompt_snapshot == analyst.system_prompt
    assert run.input_data["feedback_snapshot"] == frozen
    assert run.input_data["evidence_fingerprint"] == row.evidence_fingerprint
    assert run.input_data["executable_tools"] == []
    feedback_worker.generate_feedback_analysis.apply_async.assert_called_once()
    assert feedback_worker.generate_feedback_analysis.apply_async.call_args.kwargs["queue"] == "ai"
    assert await TaskDispatcherService(db_session).dispatch_ready_tasks() == []
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 1


@pytest.mark.parametrize("repair", [False, True])
async def test_worker_duplicate_delivery_and_structured_result_human_gate(
    db_session, monkeypatch, repair
):
    user, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    run = await dispatch(db_session, row, monkeypatch)
    calls = []

    async def execute(agent, prompt, **kwargs):
        calls.append((agent, prompt))
        await asyncio.sleep(0.02)
        return Mock(final_output=result(row, invalid=repair and len(calls) == 1))

    mock_model(monkeypatch, execute)
    await asyncio.gather(feedback_worker._generate(run.id), feedback_worker._generate(run.id))
    await feedback_worker._generate(run.id)
    await db_session.refresh(row)
    await db_session.refresh(run)
    task = await db_session.get(Task, row.task_id)
    assert len(calls) == (2 if repair else 1) == run.request_count
    assert calls[0][0].tools == [] and calls[0][0].instructions == run.prompt_snapshot
    assert canonical_json(row.input_snapshot) in calls[0][1]
    assert row.status is FeedbackAnalysisStatus.DRAFT and row.generated_at is not None
    assert row.interpretations[0]["supporting_findings"] == [0]
    assert task.status is TaskStatus.COMPLETED and run.status is AgentRunStatus.COMPLETED
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal)) == 0
    )
    await FeedbackService(db_session).review(row.id, user, FeedbackAnalysisStatus.REJECTED)
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal)) == 0
    )


@pytest.mark.parametrize("failure", ["model", "repair", "enqueue", "stuck"])
async def test_failure_all_three_retry_same_frozen_artifacts(db_session, monkeypatch, failure):
    user, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    frozen = copy.deepcopy(row.input_snapshot)
    identity, task_id, fingerprint = row.id, row.task_id, row.evidence_fingerprint
    run = await AgentRunService(db_session).create_queued_run(task_id)
    if failure == "enqueue":
        monkeypatch.setattr(
            feedback_worker.generate_feedback_analysis,
            "apply_async",
            Mock(side_effect=RuntimeError("broker unavailable")),
        )
        with pytest.raises(AppError):
            await AgentRunService(db_session).enqueue(run)
    elif failure == "stuck":
        run.status = AgentRunStatus.RUNNING
        run.started_at = datetime.now(UTC) - timedelta(days=2)
        task = await db_session.get(Task, task_id)
        task.status = TaskStatus.IN_PROGRESS
        await db_session.commit()
        await TaskRecoveryService(db_session).recover_stuck()
    else:

        async def execute(*args, **kwargs):
            if failure == "model":
                raise RuntimeError("model failure with private raw data")
            return Mock(final_output=result(row, invalid=True))

        mock_model(monkeypatch, execute)
        with pytest.raises((RuntimeError, AppError)):
            await feedback_worker._generate(run.id)
    await db_session.refresh(row)
    await db_session.refresh(run)
    task = await db_session.get(Task, task_id)
    await db_session.refresh(task)
    assert (
        row.status is FeedbackAnalysisStatus.FAILED
        and run.status is AgentRunStatus.FAILED
        and task.status is TaskStatus.FAILED
    )
    assert "private" not in (run.error_message or "")
    await append_feedback(db_session, campaign, user)

    async def retry():
        async with async_session_factory() as session:
            return await FeedbackService(session).retry_analysis(identity)

    retried, again = await asyncio.gather(retry(), retry())
    assert retried.id == again.id == identity and retried.task_id == task_id
    assert retried.input_snapshot == frozen and retried.evidence_fingerprint == fingerprint
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 1
    await db_session.refresh(row)
    new = await dispatch(db_session, row, monkeypatch)
    assert new.id != run.id and new.input_data["feedback_snapshot"] == frozen
    assert await db_session.scalar(select(func.count()).select_from(MarketingFeedbackAnalysis)) == 1


async def test_crash_window_concurrent_recovery_same_run_no_second_enqueue(db_session, monkeypatch):
    _, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    run = await AgentRunService(db_session).create_queued_run(row.task_id)
    enqueue = Mock(return_value=Mock(id="recovered-job"))
    monkeypatch.setattr(feedback_worker.generate_feedback_analysis, "apply_async", enqueue)

    async def recover():
        async with async_session_factory() as session:
            return await TaskRecoveryService(session).recover_unenqueued_optimization_runs()

    await asyncio.gather(recover(), recover())
    assert await recover() == []
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["args"] == [str(run.id)]
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 1


async def test_public_creation_and_task_edit_cannot_replace_analyst_or_evidence(db_session):
    _, campaign, *_ = await fixture(db_session)
    with pytest.raises(AppError) as error:
        await TaskService(db_session).create_task(
            TaskCreate(
                campaign_id=campaign.id, task_type=TaskType.ANALYZE_PERFORMANCE, title="Bypass"
            )
        )
    assert error.value.code == "ANALYSIS_PREPARATION_REQUIRED"
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    with pytest.raises(AppError) as error:
        await TaskService(db_session).update_task(row.task_id, TaskUpdate(input_data={}))
    assert error.value.code == "ANALYSIS_TASK_IMMUTABLE"


async def test_latest_metrics_null_exact_version_kpi_context_frozen(db_session):
    from app.models.campaign_kpi import CampaignKPI, KPIComparison, KPIMetric

    user, campaign, post, version, _ = await fixture(db_session)
    publication = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    publication = await db_session.get(Publication, publication.id)
    publication.status = PublicationStatus.PUBLISHED
    publication.published_at = datetime.now(UTC) - timedelta(days=2)
    observed = datetime.now(UTC) - timedelta(days=1)
    first = PublicationMetricsSnapshot(
        publication_id=publication.id,
        channel=post.channel,
        observed_at=observed,
        source=MetricsSource.MANUAL,
        views=0,
    )
    kpi = CampaignKPI(
        campaign_id=campaign.id,
        metric=KPIMetric.VIEWS,
        target_value=10,
        comparison=KPIComparison.GTE,
        period_start=observed - timedelta(days=2),
        period_end=observed + timedelta(days=2),
        is_active=True,
    )
    db_session.add_all([first, kpi])
    await db_session.commit()
    snapshot = await FeedbackService(db_session)._input_snapshot(campaign.id)
    assert snapshot["content_version_ids"] == [str(version.id)]
    assert snapshot["metrics"][0]["views"] == 0 and snapshot["metrics"][0]["clicks"] is None
    assert snapshot["data_quality"]["raw_metric_coverage"]["clicks"] == 0
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    frozen = copy.deepcopy(row.input_snapshot)
    row.status = FeedbackAnalysisStatus.REJECTED
    row.created_at = datetime.now(UTC) - timedelta(hours=25)
    kpi.target_value = 999
    await db_session.commit()
    assert await PerformanceAnalysisDiscoveryService(db_session).discover() == []
    second = PublicationMetricsSnapshot(
        publication_id=publication.id,
        channel=post.channel,
        observed_at=observed,
        source=MetricsSource.PROVIDER,
        provider="VK",
        views=5,
    )
    db_session.add(second)
    await db_session.commit()
    new = await FeedbackService(db_session)._input_snapshot(campaign.id)
    assert new["metrics_snapshot_ids"] == [str(second.id)]
    assert new["metrics"][0]["provider"] == "VK"
    assert evidence_fingerprint(new) != row.evidence_fingerprint
    assert row.input_snapshot == frozen and frozen["campaign_kpis"][0]["target_value"] == "10"
    assert len(await PerformanceAnalysisDiscoveryService(db_session).discover()) == 1


def test_scheduler_control_only_existing_role_queue():
    schedule = build_beat_schedule("ai", settings)
    job = schedule["discover-performance-analyses"]
    assert (
        job["task"] == "discover_performance_analyses" and job["options"]["queue"] == "ai_control"
    )
    assert job["schedule"] == 300 and TaskType.ANALYZE_PERFORMANCE in AUTO_TASK_TYPES
    assert "discover-performance-analyses" not in build_beat_schedule("publication", settings)


async def test_legacy_response_readable_and_no_retry_without_task(db_session):
    from app.tests.legacy_feedback import LegacyFeedbackFixture

    _, campaign, *_ = await fixture(db_session)
    row = await LegacyFeedbackFixture(db_session).generate_analysis(campaign.id)
    response = FeedbackAnalysisResponse.model_validate(row)
    assert (
        response.task_id is None
        and response.evidence_fingerprint is None
        and response.trigger_source is None
    )
    row.status = FeedbackAnalysisStatus.FAILED
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).retry_analysis(row.id)
    assert error.value.code == "ANALYSIS_TASK_NOT_FOUND"


@pytest.mark.parametrize("tamper", ["fingerprint", "task", "agent", "snapshot", "tools"])
async def test_worker_invalid_claim_fails_without_model(db_session, monkeypatch, tamper):
    _, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    run = await AgentRunService(db_session).create_queued_run(row.task_id)
    if tamper == "fingerprint":
        run.input_data = {**run.input_data, "evidence_fingerprint": "0" * 64}
    elif tamper == "task":
        task = await db_session.get(Task, row.task_id)
        task.input_data = {**task.input_data, "analysis_id": str(uuid4())}
    elif tamper == "agent":
        agent = await db_session.get(Agent, run.agent_id)
        agent.status = AgentStatus.INACTIVE
    elif tamper == "snapshot":
        run.input_data = {**run.input_data, "feedback_snapshot": {}}
    else:
        run.input_data = {**run.input_data, "executable_tools": ["save_content"]}
    await db_session.commit()
    model = Mock(side_effect=AssertionError("No model permitted"))
    monkeypatch.setattr(feedback_worker.Runner, "run", model)
    await feedback_worker._generate(run.id)
    await db_session.refresh(run)
    await db_session.refresh(row)
    assert (
        run.error_code == "ANALYSIS_CLAIM_INVALID" and row.status is FeedbackAnalysisStatus.FAILED
    )
    model.assert_not_called()


@pytest.mark.parametrize(
    "decision", [FeedbackAnalysisStatus.ACCEPTED, FeedbackAnalysisStatus.REJECTED]
)
async def test_completed_analysis_human_review_materializes_only_on_accept(
    db_session, monkeypatch, decision
):
    from app.tests.test_optimization import draft, recommendation

    user, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    run = await dispatch(db_session, row, monkeypatch)

    async def execute(*args, **kwargs):
        output = result(row)
        output["recommendations"] = [recommendation(draft(row.input_snapshot))]
        return Mock(final_output=output)

    mock_model(monkeypatch, execute)
    await feedback_worker._generate(run.id)
    await db_session.refresh(row)
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal)) == 0
    )
    await FeedbackService(db_session).review(row.id, user, decision)
    assert await db_session.scalar(
        select(func.count()).select_from(CampaignOptimizationProposal)
    ) == (1 if decision is FeedbackAnalysisStatus.ACCEPTED else 0)
    if decision is FeedbackAnalysisStatus.ACCEPTED:
        context = await FeedbackService(db_session).accepted_snapshot(row.id, campaign.id)
        assert context["evidence_fingerprint"] == row.evidence_fingerprint
        assert context["interpretations"] == row.interpretations
        assert context["data_quality"] == row.input_snapshot["data_quality"]
        assert context["analysis_period_start"] == row.input_snapshot["analysis_period_start"]


async def test_database_unique_fingerprint_and_restrict_task(db_session):
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    _, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    duplicate = MarketingFeedbackAnalysis(
        campaign_id=campaign.id,
        task_id=row.task_id,
        evidence_fingerprint=row.evidence_fingerprint,
        trigger_source=AnalysisTriggerSource.MANUAL,
        status=FeedbackAnalysisStatus.DRAFT,
        strategy_version=row.strategy_version,
        input_snapshot=row.input_snapshot,
        summary="",
        findings=[],
        recommendations=[],
        experiment_ideas=[],
        limitations=[],
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Task).where(Task.id == row.task_id))


async def test_discovery_batch_is_bounded(db_session):
    from app.models.campaign import Campaign
    from app.models.marketing_feedback import FeedbackCategory, FeedbackSource, MarketingFeedback

    _, campaign, *_ = await fixture(db_session)
    for index in range(3):
        other = Campaign(
            name=f"Campaign {index}",
            goal="Evidence",
            status=CampaignStatus.DRAFT,
            created_by=campaign.created_by,
        )
        db_session.add(other)
        await db_session.flush()
        db_session.add(
            MarketingFeedback(
                campaign_id=other.id,
                source_type=FeedbackSource.HUMAN,
                category=FeedbackCategory.TONE,
                comment="Evidence",
                observed_at=datetime.now(UTC),
            )
        )
    await db_session.commit()
    discovery = PerformanceAnalysisDiscoveryService(db_session)
    assert len(await discovery.discover(limit=2)) == 2
    assert len(await discovery.discover(limit=2)) == 2
    assert await discovery.discover(limit=2) == []


async def test_http_aliases_same_durable_artifact_and_status(db_session, client):
    from app.services.auth_service import AuthService

    user, campaign, *_ = await fixture(db_session)
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    base = f"/api/v1/campaigns/{campaign.id}"
    first = await client.post(base + "/feedback-analysis")
    second = await client.post(base + "/performance-analysis")
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["task_id"] and first.json()["agent_run_id"] is None
    assert (await client.get(base + "/feedback-analysis")).json() == (
        await client.get(base + "/performance-analysis")
    ).json()


async def test_activity_identifier_only_and_operational_status(db_session):
    from app.api.system import system_status
    from app.models.activity import ActivityLog

    user, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(
        campaign.id, AnalysisTriggerSource.AUTOMATIC
    )
    event = await db_session.scalar(
        select(ActivityLog).where(ActivityLog.event_type == "PERFORMANCE_ANALYSIS_DISCOVERED")
    )
    assert event is not None
    metadata = event.metadata_
    assert metadata["analysis_id"] == str(row.id) and metadata["task_id"] == str(row.task_id)
    assert "Human evidence" not in canonical_json(metadata)
    assert "input_snapshot" not in metadata and "prompt" not in metadata
    status = await system_status(user, db_session)
    assert status["performance_analysis"]["ready_tasks"] == 1
    assert status["performance_analysis"]["latest_automatic_analysis_at"] is not None


@pytest.mark.parametrize("race", ["discovery-discovery", "discovery-http-core"])
async def test_real_discovery_services_compete_with_same_core(db_session, race):
    _, campaign, *_ = await fixture(db_session)
    campaign_id = campaign.id

    async def discover():
        async with async_session_factory() as session:
            return await PerformanceAnalysisDiscoveryService(session).discover()

    async def manual():
        async with async_session_factory() as session:
            return await FeedbackService(session).prepare_analysis(campaign_id)

    await asyncio.gather(discover(), discover() if race == "discovery-discovery" else manual())
    assert await db_session.scalar(select(func.count()).select_from(MarketingFeedbackAnalysis)) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.task_type == TaskType.ANALYZE_PERFORMANCE)
        )
        == 1
    )
    assert await db_session.scalar(select(func.count()).select_from(AgentRun)) == 0


async def test_ordinary_content_revision_unsubmitted_recovery(db_session, monkeypatch):
    from app.tests.test_scheduler_recovery import revision_fixture
    from app.workers.agent_worker import execute_agent_run

    _, _, _, task = await revision_fixture(db_session)
    run = await AgentRunService(db_session).create_queued_run(task.id)
    enqueue = Mock(return_value=Mock(id="ordinary-revision-recovery"))
    monkeypatch.setattr(execute_agent_run, "apply_async", enqueue)
    recovered = await TaskRecoveryService(db_session).recover_unenqueued_optimization_runs()
    assert [item.id for item in recovered] == [run.id]
    assert await TaskRecoveryService(db_session).recover_unenqueued_optimization_runs() == []
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["queue"] == "ai"


async def test_retry_old_failed_delivery_cannot_touch_new_run(db_session, monkeypatch):
    _, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    old = await AgentRunService(db_session).create_queued_run(row.task_id)
    old.status = AgentRunStatus.FAILED
    row.status = FeedbackAnalysisStatus.FAILED
    task = await db_session.get(Task, row.task_id)
    task.status = TaskStatus.FAILED
    await db_session.commit()
    await FeedbackService(db_session).retry_analysis(row.id)
    new = await dispatch(db_session, row, monkeypatch)
    model = Mock(side_effect=AssertionError("Old run cannot execute"))
    monkeypatch.setattr(feedback_worker.Runner, "run", model)
    await feedback_worker._generate(old.id)
    await db_session.refresh(row)
    await db_session.refresh(new)
    assert row.agent_run_id == new.id and new.status is AgentRunStatus.QUEUED
    assert row.status is FeedbackAnalysisStatus.DRAFT
    model.assert_not_called()


async def test_seed_fifth_agent_zero_tools_idempotent_authoritative(db_session):
    from app.repositories.agents import AgentRepository
    from app.seed import seed_agents

    assert await seed_agents() == (5, 15)
    agent = await AgentRepository(db_session).get_by_slug("marketing_analyst", with_tools=True)
    assert agent.role == "marketing_analyst" and agent.tools == []
    assert agent.system_prompt == Path("app/prompts/marketing_analyst.md").read_text()
    agent.system_prompt = "User-maintained analyst instructions"
    await db_session.commit()
    assert await seed_agents() == (0, 0)
    await db_session.refresh(agent)
    assert agent.system_prompt == "User-maintained analyst instructions"


@pytest.mark.parametrize(
    "kind", ["publication", "content_version", "metrics_snapshot", "marketing_feedback"]
)
def test_each_reference_type_must_be_inside_frozen_allowlist(kind):
    identity = str(uuid4())
    keys = {
        "publication": "publication_ids",
        "content_version": "content_version_ids",
        "metrics_snapshot": "metrics_snapshot_ids",
        "marketing_feedback": "feedback_ids",
    }
    snapshot = {key: [] for key in keys.values()}
    snapshot[keys[kind]] = [identity]
    payload = {
        "summary": "Evidence",
        "findings": [
            {
                "type": "OBSERVATION",
                "observation": "Observed",
                "evidence_refs": [{"type": kind, "id": identity}],
                "confidence": "low",
            }
        ],
        "recommendations": [],
        "experiment_ideas": [],
        "limitations": [],
    }
    FeedbackService.validate_evidence(snapshot, FeedbackAnalystResult.model_validate(payload))
    payload["findings"][0]["evidence_refs"][0]["id"] = str(uuid4())
    with pytest.raises(AppError) as error:
        FeedbackService.validate_evidence(snapshot, FeedbackAnalystResult.model_validate(payload))
    assert error.value.code == "FEEDBACK_EVIDENCE_INVALID"


async def test_latest_snapshot_identical_timestamps_uses_id_tiebreaker(db_session):
    from app.models.publication import Publication

    user, campaign, post, version, _ = await fixture(db_session)
    response = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    publication = await db_session.get(Publication, response.id)
    publication.status = PublicationStatus.PUBLISHED
    publication.published_at = datetime.now(UTC) - timedelta(days=2)
    timestamp = datetime.now(UTC) - timedelta(days=1)
    first, second = sorted([uuid4(), uuid4()])
    for identity in (first, second):
        db_session.add(
            PublicationMetricsSnapshot(
                id=identity,
                publication_id=publication.id,
                channel=post.channel,
                observed_at=timestamp,
                created_at=timestamp,
                source=MetricsSource.MANUAL,
            )
        )
    await db_session.commit()
    one = await FeedbackService(db_session)._input_snapshot(campaign.id)
    two = await FeedbackService(db_session)._input_snapshot(campaign.id)
    assert one["metrics_snapshot_ids"] == [str(second)]
    assert canonical_json(one) == canonical_json(two)
    assert one["metrics"][0]["views"] is None


async def test_new_evidence_old_created_timestamp_still_discovered(db_session):
    user, campaign, *_ = await fixture(db_session)
    row = await FeedbackService(db_session).prepare_analysis(campaign.id)
    row.status = FeedbackAnalysisStatus.REJECTED
    row.created_at = datetime.now(UTC) - timedelta(hours=25)
    await db_session.commit()
    feedback = await append_feedback(db_session, campaign, user)
    # Simulate an older transaction that commits after the previous analysis.
    feedback.created_at = datetime.now(UTC) - timedelta(hours=26)
    await db_session.commit()
    assert len(await PerformanceAnalysisDiscoveryService(db_session).discover()) == 1
