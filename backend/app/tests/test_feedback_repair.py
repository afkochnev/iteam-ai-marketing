"""Sparse production-like evidence; all model/network execution is mocked."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from agents import ModelBehaviorError
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign_kpi import CampaignKPI, KPIComparison, KPIMetric
from app.models.content import ContentChannel, ContentItem, ContentVersion
from app.models.marketing_feedback import FeedbackAnalysisStatus
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionStatus,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.models.publication_plan import PublicationPlanItem
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.feedback import FeedbackAnalystResult
from app.services.feedback_analysis_validation import (
    ValidationCode,
    classify_feedback_analysis_validation_error,
    feedback_analysis_constraints,
    feedback_analysis_repair_instruction,
)
from app.services.feedback_service import FeedbackService
from app.services.optimization_proposal_service import OptimizationProposalService
from app.services.optimization_workspace_service import OptimizationWorkspaceService
from app.services.performance_evidence import canonical_json
from app.tests.test_campaign_workspace import workspace_fixture
from app.tests.test_optimization import draft, recommendation
from app.tests.test_performance_analysis import dispatch, mock_model, result
from app.workers import feedback_worker

PRIVATE = "PRIVATE_FEEDBACK_SECRET_123"


async def sparse_fixture(session):
    data = await workspace_fixture(session)
    campaign, post = data["campaign"], data["post"]
    user = await session.get(User, campaign.created_by)
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
    session.add(analyst)
    now = datetime.now(UTC)
    publication = data["publication"]
    publication.status = PublicationStatus.PUBLISHED
    publication.published_at = now - timedelta(days=2)
    for index in range(2, 9):
        source = data["plan_item"]
        slot = PublicationPlanItem(
            **{
                c.name: getattr(source, c.name)
                for c in PublicationPlanItem.__table__.columns
                if c.name not in {"id", "created_at", "updated_at", "position"}
            },
            position=index,
        )
        session.add(slot)
        await session.flush()
        item = ContentItem(
            campaign_id=campaign.id,
            source_task_id=post.source_task_id,
            content_type=post.content_type,
            title=f"Exact post {index}",
            status=post.status,
            author_agent_id=post.author_agent_id,
            channel=post.channel,
            metadata_={
                "publication_plan_item_id": str(slot.id),
                "publication_plan_id": str(data["plan"].id),
                "source_content_version_id": str(data["article_version"].id),
            },
        )
        session.add(item)
        await session.flush()
        exact = ContentVersion(
            content_item_id=item.id,
            version_number=1,
            content="Frozen exact payload",
            structured_content={},
        )
        session.add(exact)
        await session.flush()
        item.current_version_id = exact.id
        session.add(
            Publication(
                campaign_id=campaign.id,
                content_item_id=item.id,
                content_version_id=exact.id,
                channel=post.channel,
                status=PublicationStatus.PUBLISHED,
                published_at=now - timedelta(days=2),
            )
        )
    session.add_all(
        [
            PublicationMetricsSnapshot(
                publication_id=publication.id,
                channel=post.channel,
                observed_at=now - timedelta(days=1),
                source=MetricsSource.MANUAL,
                views=10,
            ),
            CampaignKPI(
                campaign_id=campaign.id,
                metric=KPIMetric.VIEWS,
                channel=ContentChannel.TELEGRAM,
                target_value=100,
                comparison=KPIComparison.GTE,
                period_start=now - timedelta(days=7),
                period_end=now + timedelta(days=7),
                is_active=True,
            ),
        ]
    )
    await session.commit()
    await FeedbackService(session).create_feedback(
        campaign.id, user, {"category": "TONE", "comment": "Realistic sparse human observation"}
    )
    row = await FeedbackService(session).prepare_analysis(campaign.id, automatic=True)
    assert row.input_snapshot["publications_analyzed"] == 8
    assert len(row.input_snapshot["metrics_snapshot_ids"]) == 1
    assert len(row.input_snapshot["feedback_ids"]) == 1
    assert row.input_snapshot["metric_coverage_ratio"] == 1 / 8
    assert row.input_snapshot["optimization_target_allowlist"]["publication_plans"]
    return user, row


def output(row, kind="NO_CHANGE"):
    payload = result(row)
    overrides = {"evidence_refs": payload["findings"][0]["evidence_refs"]}
    if kind == "CONTENT_REVISION":
        target = row.input_snapshot["optimization_target_allowlist"]["content"][0]
        overrides.update(
            target_entity_type="CONTENT_ITEM",
            target_entity_id=target["content_item_id"],
            target_version_id=target["content_version_id"],
        )
    elif kind == "PUBLICATION_PLAN_REVISION":
        target = row.input_snapshot["optimization_target_allowlist"]["publication_plans"][0]
        overrides.update(target_entity_type="PUBLICATION_PLAN", target_entity_id=target["id"])
    payload["recommendations"] = [recommendation(draft(row.input_snapshot, kind, **overrides))]
    return payload


def invalid_output(row, case):
    value = output(row)
    if case == "evidence":
        value["findings"][0]["evidence_refs"][0]["id"] = str(uuid4())
    elif case in {"content_target", "content_version", "plan_target"}:
        value = output(
            row, "PUBLICATION_PLAN_REVISION" if case == "plan_target" else "CONTENT_REVISION"
        )
        action = value["recommendations"][0]["proposed_action"]
        action["target_version_id" if case == "content_version" else "target_entity_id"] = str(
            uuid4()
        )
    elif case == "interpretation":
        value["interpretations"][0]["supporting_findings"] = [9]
    elif case in {"experiment", "experiment_missing"}:
        value = output(row, "EXPERIMENT")
        action = value["recommendations"][0]["proposed_action"]
        if case == "experiment_missing":
            action["experiment_spec"] = None
        else:
            action["experiment_spec"]["success_metric"] = PRIVATE
    else:
        value[PRIVATE] = PRIVATE
    value["summary"] = PRIVATE  # Invalid output must never become an error diagnostic.
    return value


CASES = [
    ("evidence", "EVIDENCE_REF_INVALID"),
    ("content_target", "OPTIMIZATION_TARGET_INVALID"),
    ("content_version", "OPTIMIZATION_TARGET_INVALID"),
    ("plan_target", "OPTIMIZATION_TARGET_INVALID"),
    ("interpretation", "INTERPRETATION_INDEX_INVALID"),
    ("experiment", "EXPERIMENT_SPEC_INVALID"),
    ("experiment_missing", "EXPERIMENT_SPEC_INVALID"),
    ("schema", "SCHEMA_INVALID"),
    ("model", "MODEL_BEHAVIOR_INVALID"),
]


@pytest.mark.parametrize("case,code", CASES)
@pytest.mark.parametrize("exhausted", [False, True])
async def test_category_repair_or_exhaustion(db_session, monkeypatch, case, code, exhausted):
    _, row = await sparse_fixture(db_session)
    frozen, fingerprint, task_id = (
        deepcopy(row.input_snapshot),
        row.evidence_fingerprint,
        row.task_id,
    )
    run = await dispatch(db_session, row, monkeypatch)
    prompts = []

    async def execute(agent, prompt, **kwargs):
        prompts.append(prompt)
        assert agent.tools == [] and kwargs["max_turns"] == 1
        if len(prompts) == 1 or exhausted:
            if case == "model":
                raise ModelBehaviorError(PRIVATE)
            return Mock(final_output=invalid_output(row, case))
        return Mock(final_output=output(row))

    mock_model(monkeypatch, execute)
    fake_client = feedback_worker.AsyncOpenAI
    client_options = []

    def client(**kwargs):
        client_options.append(kwargs)
        return fake_client(**kwargs)

    monkeypatch.setattr(feedback_worker, "AsyncOpenAI", client)
    if exhausted:
        with pytest.raises(AppError) as error:
            await feedback_worker._generate(run.id)
        assert error.value.code == "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED"
    else:
        await feedback_worker._generate(run.id)
    await db_session.refresh(row)
    await db_session.refresh(run)
    task = await db_session.get(Task, task_id)
    await db_session.refresh(task)
    assert run.request_count == len(prompts) == 2
    assert client_options[0]["max_retries"] == 0
    assert run.input_data["model_request_accounting"]["external_model_request_count"] == 2
    assert all("Application constraints" in prompt for prompt in prompts)
    assert run.output_data["last_validation_code"] == code
    assert run.output_data["validation_failures"][0]["attempt_number"] == 1
    assert run.output_data["validation_failures"][0]["validation_code"] == code
    assert row.input_snapshot == frozen and row.evidence_fingerprint == fingerprint
    assert row.task_id == task_id
    assert code in prompts[1] and PRIVATE not in prompts[1]
    if exhausted:
        assert row.status is FeedbackAnalysisStatus.FAILED
        assert run.status is AgentRunStatus.FAILED and task.status is TaskStatus.FAILED
        assert run.error_code == "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED"
        assert run.output_data["final_validation_state"] == run.error_code
        assert run.output_data["validation_failures"][1]["attempt_number"] == 2
        assert row.generated_at is None and row.recommendations == []
    else:
        assert row.status is FeedbackAnalysisStatus.DRAFT and row.generated_at is not None
        assert run.status is AgentRunStatus.COMPLETED and task.status is TaskStatus.COMPLETED
    for model in [CampaignOptimizationProposal, CampaignOptimizationAction]:
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0
    metadata = list(await db_session.scalars(select(ActivityLog.metadata_)))
    assert PRIVATE not in canonical_json([run.output_data, metadata])
    assert PRIVATE not in (run.error_message or "") + (task.error_message or "")
    if exhausted:
        identity = row.id
        await FeedbackService(db_session).retry_analysis(identity)
        await db_session.refresh(row)
        assert row.id == identity and row.task_id == task_id
        assert row.input_snapshot == frozen and row.evidence_fingerprint == fingerprint
        new = await dispatch(db_session, row, monkeypatch)
        assert new.id != run.id and new.input_data["feedback_snapshot"] == frozen
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task_id)
            )
            == 2
        )


@pytest.mark.parametrize("empty", [False, True])
async def test_sparse_no_change_or_empty_human_accept(db_session, monkeypatch, empty):
    user, row = await sparse_fixture(db_session)
    run = await dispatch(db_session, row, monkeypatch)
    payload = result(row) if empty else output(row)
    if not empty:
        action = payload["recommendations"][0]["proposed_action"]
        assert action["type"] == "NO_CHANGE" and action["target_entity_type"] == "CAMPAIGN"
        assert action["target_entity_id"] == str(row.campaign_id)
        assert action["target_version_id"] is None and action["experiment_spec"] is None

    async def execute(*args, **kwargs):
        return Mock(final_output=payload)

    mock_model(monkeypatch, execute)
    await feedback_worker._generate(run.id)
    await db_session.refresh(run)
    await db_session.refresh(row)
    assert run.request_count == 1 and row.status is FeedbackAnalysisStatus.DRAFT
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal)) == 0
    )
    await FeedbackService(db_session).review(row.id, user, FeedbackAnalysisStatus.ACCEPTED)
    assert await db_session.scalar(
        select(func.count()).select_from(CampaignOptimizationProposal)
    ) == (0 if empty else 1)
    actions = list(await db_session.scalars(select(CampaignOptimizationAction)))
    assert (
        not actions
        if empty
        else all(a.type.value == "NO_CHANGE" and a.status.value == "PROPOSED" for a in actions)
    )

    if not empty:
        await OptimizationProposalService(db_session).decide(
            actions[0].id, user, OptimizationActionStatus.APPROVED
        )
        state = (await OptimizationWorkspaceService(db_session).states([row.campaign_id]))[
            row.campaign_id
        ]
        assert state.apply_actions == []  # NO_CHANGE never requires a business Apply.


async def test_constraint_fragments_and_safe_repair_prompts(db_session):
    _, row = await sparse_fixture(db_session)
    prompt = feedback_analysis_constraints(row.input_snapshot)
    for key in ["publication_ids", "content_version_ids", "metrics_snapshot_ids", "feedback_ids"]:
        assert all(identity in prompt for identity in row.input_snapshot[key])
    allowed = row.input_snapshot["optimization_target_allowlist"]
    assert str(row.campaign_id) in prompt and '"strategy_version":' in prompt
    for target in allowed["content"]:
        assert target["content_item_id"] in prompt and target["content_version_id"] in prompt
    assert all(target["id"] in prompt for target in allowed["publication_plans"])
    for fragment in [
        "NO_CHANGE",
        "CAMPAIGN",
        '"target_version_id":null',
        "zero-based",
        "recommendations = []",
    ]:
        assert fragment in prompt
    assert "tool" not in prompt.lower()
    for error in [
        AppError("FEEDBACK_EVIDENCE_INVALID", PRIVATE, 422),
        AppError("OPTIMIZATION_ACTION_INVALID", PRIVATE, 422),
        ModelBehaviorError(PRIVATE),
    ]:
        failure = classify_feedback_analysis_validation_error(error)
        assert PRIVATE not in feedback_analysis_repair_instruction(failure)
        assert PRIVATE not in str(failure)


@pytest.mark.parametrize("case,code", CASES[:-1])
def test_structured_classifier_never_error_prose(case, code):
    snapshot = {
        "campaign_id": str(uuid4()),
        "feedback_ids": [str(uuid4())],
        "optimization_target_allowlist": {"campaign": {"id": str(uuid4())}},
    }
    row = Mock(input_snapshot=snapshot)
    if case in {"evidence", "content_target", "content_version", "plan_target"}:
        error = AppError(
            "FEEDBACK_EVIDENCE_INVALID" if case == "evidence" else "OPTIMIZATION_ACTION_INVALID",
            PRIVATE,
            422,
        )
    else:
        with pytest.raises(ValidationError) as caught:
            FeedbackAnalystResult.model_validate(invalid_output(row, case))
        error = caught.value
    failure = classify_feedback_analysis_validation_error(error)
    assert failure.validation_code == ValidationCode(code)
    assert PRIVATE not in feedback_analysis_repair_instruction(failure)
