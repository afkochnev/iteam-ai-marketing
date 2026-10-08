import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import CampaignStatus
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
)
from app.models.optimization import (
    OptimizationActionStatus as AS,
)
from app.models.optimization import (
    OptimizationActionType as AT,
)
from app.models.optimization import (
    OptimizationProposalStatus as PS,
)
from app.models.publication_plan import PublicationPlan, PublicationPlanStatus
from app.schemas.feedback import FeedbackAnalystResult, OptimizationActionDraft
from app.schemas.publication import PublicationCreate
from app.services.auth_service import AuthService
from app.services.feedback_service import FeedbackService
from app.services.optimization_proposal_service import OptimizationProposalService, validate_action
from app.services.publication_service import PublicationService
from app.tests.test_agent_runtime import runtime_fixture
from app.tests.test_publications import _approved_post


def snapshot(campaign_id):
    return {
        "campaign_id": str(campaign_id),
        "strategy_version": 7,
        "publication_ids": [],
        "content_version_ids": [],
        "metrics_snapshot_ids": [],
        "feedback_ids": [],
        "optimization_target_allowlist": {
            "campaign": {"id": str(campaign_id), "strategy_version": 7},
            "content": [],
            "publication_plans": [],
        },
    }


def draft(s, kind="NO_CHANGE", **overrides):
    return {
        "type": kind,
        "target_entity_type": "CAMPAIGN_STRATEGY" if kind == "STRATEGY_REVIEW" else "CAMPAIGN",
        "target_entity_id": s["campaign_id"],
        "target_version_id": None,
        "reason": "Наблюдения недостаточны",
        "expected_effect": "Сохранить проверенный подход",
        "priority": "LOW",
        "evidence_refs": [],
        **overrides,
    }


def recommendation(action):
    return {
        "category": "EDITORIAL",
        "recommendation": "Консультативный вывод",
        "expected_effect": action["expected_effect"],
        "priority": action["priority"],
        "evidence_refs": action["evidence_refs"],
        "proposed_action": action,
    }


async def typed_analysis(session, count=2):
    user, agent, campaign, task = await runtime_fixture(session)
    s = snapshot(campaign.id)
    run = AgentRun(
        agent_id=agent.id,
        task_id=task.id,
        campaign_id=campaign.id,
        status=AgentRunStatus.COMPLETED,
        model="test-model",
        prompt_snapshot="test",
        prompt_hash="test",
        input_data={},
        output_data={},
    )
    session.add(run)
    await session.flush()
    analysis = MarketingFeedbackAnalysis(
        campaign_id=campaign.id,
        strategy_version=7,
        status=FeedbackAnalysisStatus.DRAFT,
        summary="Выводы по замороженному контексту",
        input_snapshot=s,
        findings=[],
        recommendations=[recommendation(draft(s)) for _ in range(count)],
        experiment_ideas=[],
        limitations=[],
        agent_run_id=run.id,
    )
    session.add(analysis)
    await session.commit()
    return user, campaign, analysis


async def accepted(session, count=2):
    user, campaign, analysis = await typed_analysis(session, count)
    await FeedbackService(session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    proposal = await session.scalar(
        select(CampaignOptimizationProposal).where(
            CampaignOptimizationProposal.feedback_analysis_id == analysis.id
        )
    )
    return user, campaign, analysis, proposal


async def artifacts(session):
    return {
        table: (
            await session.execute(
                text(
                    "SELECT count(*), md5(COALESCE(string_agg(to_jsonb(t)::text, "
                    f"E'\\n' ORDER BY to_jsonb(t)::text),'')) FROM {table} t"
                )
            )
        ).one()
        for table in [
            "tasks",
            "agent_runs",
            "content_versions",
            "publication_plans",
            "publication_plan_items",
            "publications",
            "campaigns",
        ]
    }


async def test_materialization_atomic_exact_provenance_idempotent_no_model_or_artifacts(
    db_session, monkeypatch
):
    from app.workers import feedback_worker

    async def forbidden(*args, **kwargs):
        raise AssertionError("A second model call is forbidden")

    monkeypatch.setattr(feedback_worker.Runner, "run", forbidden)
    user, campaign, analysis = await typed_analysis(db_session)
    before = await artifacts(db_session)
    service = FeedbackService(db_session)
    await service.review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    await service.review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    proposals = await OptimizationProposalService(db_session).list(campaign.id)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert (
        proposal.strategy_version == 7 and proposal.created_by_agent_run_id == analysis.agent_run_id
    )
    assert proposal.status is PS.WAITING_APPROVAL
    assert len(proposal.actions) == 2
    assert [a.position for a in proposal.actions] == [0, 1]
    assert [a.source_recommendation_index for a in proposal.actions] == [0, 1]
    assert proposal.reviewed_at is None
    assert before == await artifacts(db_session)
    events = list(
        await db_session.scalars(
            select(ActivityLog).where(ActivityLog.event_type == "OPTIMIZATION_PROPOSAL_CREATED")
        )
    )
    assert len(events) == 1 and events[0].user_id == user.id
    assert events[0].metadata_ == {
        "proposal_id": str(proposal.id),
        "feedback_analysis_id": str(analysis.id),
        "status": "WAITING_APPROVAL",
    }


@pytest.mark.parametrize("constraint", ["analysis", "position", "recommendation"])
async def test_postgresql_uniqueness(db_session, constraint):
    _user, _campaign, analysis, proposal = await accepted(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            if constraint == "analysis":
                db_session.add(
                    CampaignOptimizationProposal(
                        campaign_id=proposal.campaign_id,
                        feedback_analysis_id=analysis.id,
                        strategy_version=7,
                        status=PS.WAITING_APPROVAL,
                        summary="duplicate",
                    )
                )
            else:
                original = proposal.actions[0]
                db_session.add(
                    CampaignOptimizationAction(
                        proposal_id=proposal.id,
                        position=original.position if constraint == "position" else 99,
                        source_recommendation_index=original.source_recommendation_index
                        if constraint == "recommendation"
                        else 99,
                        **draft(analysis.input_snapshot),
                    )
                )
            await db_session.flush()


@pytest.mark.parametrize("legacy", ["old", "empty"])
async def test_legacy_accept_has_no_fake_proposal(db_session, legacy):
    user, campaign, analysis = await typed_analysis(db_session)
    analysis.input_snapshot = {
        k: v for k, v in analysis.input_snapshot.items() if k != "optimization_target_allowlist"
    }
    analysis.recommendations = [{"recommendation": "Old recommendation"}] if legacy == "old" else []
    await db_session.commit()
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    assert analysis.status is FeedbackAnalysisStatus.ACCEPTED
    assert await OptimizationProposalService(db_session).list(campaign.id) == []
    assert "optimization_target_allowlist" not in analysis.input_snapshot


@pytest.mark.parametrize(
    "problem",
    ["mixed", "target", "evidence", "outer_evidence", "strategy", "campaign", "unknown_type"],
)
async def test_invalid_accept_persists_nothing(db_session, problem):
    user, _campaign, analysis = await typed_analysis(db_session)
    recs = deepcopy(analysis.recommendations)
    if problem == "mixed":
        recs[1].pop("proposed_action")
    elif problem == "target":
        recs[1]["proposed_action"]["target_entity_id"] = str(uuid4())
    elif problem == "evidence":
        recs[1]["proposed_action"]["evidence_refs"] = [{"type": "publication", "id": str(uuid4())}]
    elif problem == "outer_evidence":
        recs[1]["evidence_refs"] = [{"type": "publication", "id": str(uuid4())}]
    elif problem == "unknown_type":
        recs[1]["proposed_action"]["type"] = "PUBLISH_NOW"
    elif problem == "strategy":
        analysis.strategy_version = 99
    else:
        analysis.input_snapshot = {**analysis.input_snapshot, "campaign_id": str(uuid4())}
    analysis.recommendations = recs
    await db_session.commit()
    identity = analysis.id
    with pytest.raises(AppError):
        await FeedbackService(db_session).review(identity, user, FeedbackAnalysisStatus.ACCEPTED)
    await db_session.rollback()
    row = await db_session.get(MarketingFeedbackAnalysis, identity)
    assert row.status is FeedbackAnalysisStatus.DRAFT
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal)) == 0
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationAction)) == 0
    )


@pytest.mark.parametrize("kind", list(AT))
def test_each_action_validates_frozen_target(kind):
    s = snapshot(uuid4())
    item, version, plan = map(str, [uuid4(), uuid4(), uuid4()])
    s["optimization_target_allowlist"]["content"] = [
        {"campaign_id": s["campaign_id"], "content_item_id": item, "content_version_id": version}
    ]
    s["optimization_target_allowlist"]["publication_plans"] = [
        {"id": plan, "campaign_id": s["campaign_id"], "status": "APPROVED"}
    ]
    fields = (
        {
            "target_entity_type": "CONTENT_ITEM",
            "target_entity_id": item,
            "target_version_id": version,
        }
        if kind is AT.CONTENT_REVISION
        else {"target_entity_type": "PUBLICATION_PLAN", "target_entity_id": plan}
        if kind is AT.PUBLICATION_PLAN_REVISION
        else {}
    )
    action = OptimizationActionDraft.model_validate(draft(s, kind, **fields))
    validate_action(s, action)
    for overrides in [
        {"target_entity_id": uuid4()},
        {
            "target_entity_type": "PUBLICATION_PLAN"
            if kind is not AT.PUBLICATION_PLAN_REVISION
            else "CAMPAIGN"
        },
        {"target_version_id": None if kind is AT.CONTENT_REVISION else uuid4()},
    ]:
        with pytest.raises(AppError):
            validate_action(s, action.model_copy(update=overrides))
    if kind in {AT.CONTENT_REVISION, AT.PUBLICATION_PLAN_REVISION}:
        bad = deepcopy(s)
        key = "content" if kind is AT.CONTENT_REVISION else "publication_plans"
        bad["optimization_target_allowlist"][key][0]["campaign_id"] = str(uuid4())
        with pytest.raises(AppError):
            validate_action(bad, action)
    if kind is AT.CONTENT_REVISION:
        with pytest.raises(AppError):
            validate_action(s, action.model_copy(update={"target_version_id": uuid4()}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "arbitrary"),
        ("target_entity_type", "URL"),
        ("reason", ""),
        ("priority", "x" * 41),
        ("target_entity_id", "not-uuid"),
        ("href", "https://evil.test"),
    ],
)
def test_strict_draft(field, value):
    with pytest.raises(ValidationError):
        OptimizationActionDraft.model_validate(draft(snapshot(uuid4()), **{field: value}))


def test_new_recommendations_require_proposed_action():
    row = recommendation(draft(snapshot(uuid4())))
    row.pop("proposed_action")
    with pytest.raises(ValidationError):
        FeedbackAnalystResult.model_validate(
            {
                "summary": "test",
                "findings": [],
                "recommendations": [row],
                "experiment_ideas": [],
                "limitations": [],
            }
        )


@pytest.mark.parametrize("decision", [AS.APPROVED, AS.REJECTED])
async def test_decision_idempotent_opposite_conflict_no_business_artifacts(db_session, decision):
    user, _campaign, analysis, proposal = await accepted(db_session)
    before = await artifacts(db_session)
    aid = proposal.actions[0].id
    service = OptimizationProposalService(db_session)
    await service.decide(aid, user, decision)
    await service.decide(aid, user, decision)
    opposite = AS.REJECTED if decision is AS.APPROVED else AS.APPROVED
    with pytest.raises(AppError) as error:
        await service.decide(aid, user, opposite)
    assert error.value.status_code == 409
    assert before == await artifacts(db_session)
    events = list(
        await db_session.scalars(
            select(ActivityLog).where(
                ActivityLog.event_type == f"OPTIMIZATION_ACTION_{decision.value}"
            )
        )
    )
    assert len(events) == 1 and events[0].user_id == user.id
    assert events[0].metadata_ == {
        "proposal_id": str(proposal.id),
        "action_id": str(aid),
        "feedback_analysis_id": str(analysis.id),
        "action_type": "NO_CHANGE",
        "status": decision.value,
    }


@pytest.mark.parametrize(
    "decisions,expected",
    [
        ([AS.APPROVED, AS.APPROVED], PS.APPROVED),
        ([AS.REJECTED, AS.REJECTED], PS.REJECTED),
        ([AS.APPROVED, AS.REJECTED], PS.PARTIALLY_APPROVED),
    ],
)
async def test_aggregate_terminal_review_metadata(db_session, decisions, expected):
    user, _campaign, _analysis, proposal = await accepted(db_session)
    ids = [a.id for a in proposal.actions]
    service = OptimizationProposalService(db_session)
    await service.decide(ids[0], user, decisions[0])
    await db_session.refresh(proposal)
    assert proposal.status is PS.PARTIALLY_APPROVED and proposal.reviewed_at is None
    await service.decide(ids[1], user, decisions[1])
    await db_session.refresh(proposal)
    assert (
        proposal.status is expected
        and proposal.reviewed_at
        and proposal.reviewed_by_user_id == user.id
    )


async def test_concurrent_opposite_decisions_exactly_one_winner(db_session):
    user, _campaign, _analysis, proposal = await accepted(db_session, 1)
    uid, aid, pid = user.id, proposal.actions[0].id, proposal.id
    await db_session.commit()

    async def vote(decision):
        async with async_session_factory() as session:
            voter = await session.get(type(user), uid)
            try:
                return (
                    await OptimizationProposalService(session).decide(aid, voter, decision)
                ).status
            except AppError as exc:
                return exc.status_code

    results = await asyncio.gather(vote(AS.APPROVED), vote(AS.REJECTED))
    assert results.count(409) == 1
    await db_session.refresh(proposal)
    assert proposal.status in {PS.APPROVED, PS.REJECTED}
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.operation_key == f"optimization-action-decision:{aid}")
        )
        == 1
    )
    assert pid == proposal.id


async def test_concurrent_sibling_decisions_aggregate_without_lost_update(db_session):
    user, _campaign, _analysis, proposal = await accepted(db_session)
    uid, ids = user.id, [a.id for a in proposal.actions]
    await db_session.commit()

    async def vote(aid):
        async with async_session_factory() as session:
            await OptimizationProposalService(session).decide(
                aid, await session.get(type(user), uid), AS.APPROVED
            )

    await asyncio.gather(*(vote(aid) for aid in ids))
    await db_session.refresh(proposal)
    assert proposal.status is PS.APPROVED and proposal.reviewed_at


async def test_api_auth_archived_readonly(db_session, client: AsyncClient):
    user, campaign, analysis, proposal = await accepted(db_session)
    pid, aid, cid = proposal.id, proposal.actions[0].id, campaign.id
    for url in [
        f"/campaigns/{cid}/optimization-proposals",
        f"/optimization-proposals/{pid}",
        f"/optimization-actions/{aid}/approve",
        f"/optimization-actions/{aid}/reject",
    ]:
        response = await (client.post if url.endswith(("approve", "reject")) else client.get)(
            "/api/v1" + url
        )
        assert response.status_code == 401
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    assert (await client.get(f"/api/v1/optimization-proposals/{pid}")).json()["actions"][0][
        "status"
    ] == "PROPOSED"
    campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    assert (await client.get(f"/api/v1/campaigns/{cid}/optimization-proposals")).status_code == 200
    assert (await client.get(f"/api/v1/optimization-proposals/{pid}")).status_code == 200
    for decision in ["approve", "reject"]:
        assert (
            await client.post(f"/api/v1/optimization-actions/{aid}/{decision}")
        ).status_code == 409
    assert (
        await client.post(f"/api/v1/optimization-actions/{aid}/apply", json={})
    ).status_code == 409
    assert analysis.status is FeedbackAnalysisStatus.ACCEPTED


async def test_snapshot_freezes_real_content_version_and_plan_state(db_session):
    user, campaign, post, version = await _approved_post(db_session)
    await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=datetime.now(UTC),
        planning_horizon_end=datetime.now(UTC) + timedelta(days=7),
        created_by_user_id=user.id,
    )
    db_session.add(plan)
    await db_session.commit()
    service = FeedbackService(db_session)
    frozen = await service._input_snapshot(campaign.id)
    assert frozen == await service._input_snapshot(campaign.id)
    allowlist = frozen["optimization_target_allowlist"]
    assert {
        "campaign_id": str(campaign.id),
        "content_item_id": str(post.id),
        "content_version_id": str(version.id),
    } in allowlist["content"]
    assert allowlist["publication_plans"][0]["id"] == str(plan.id)
    assert allowlist["publication_plans"][0]["status"] == "APPROVED"
    campaign.strategy_version += 1
    plan.status = PublicationPlanStatus.ARCHIVED
    await db_session.commit()
    validate_action(
        frozen,
        OptimizationActionDraft.model_validate(
            draft(
                frozen,
                "CONTENT_REVISION",
                target_entity_type="CONTENT_ITEM",
                target_entity_id=str(post.id),
                target_version_id=str(version.id),
            )
        ),
    )
    validate_action(
        frozen,
        OptimizationActionDraft.model_validate(
            draft(
                frozen,
                "PUBLICATION_PLAN_REVISION",
                target_entity_type="PUBLICATION_PLAN",
                target_entity_id=str(plan.id),
            )
        ),
    )
    assert frozen["strategy_version"] != campaign.strategy_version
    assert allowlist["publication_plans"][0]["status"] == "APPROVED"


async def test_migration_tables_enums_and_restrict_provenance(db_session):
    assert (
        await db_session.scalar(text("SELECT version_num FROM alembic_version")) == "20261007_0024"
    )
    enums = list(
        await db_session.scalars(
            text("SELECT typname FROM pg_type WHERE typname LIKE 'optimization_%' AND typtype='e'")
        )
    )
    assert len(enums) == 4
    _user, _campaign, analysis, _proposal = await accepted(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.delete(analysis)
            await db_session.flush()


async def test_concurrent_accept_one_durable_proposal(db_session):
    user, _campaign, analysis = await typed_analysis(db_session)
    uid, analysis_id = user.id, analysis.id

    ready = asyncio.Event()
    loaded = 0

    async def accept():
        nonlocal loaded
        async with async_session_factory() as session:
            voter = await session.get(type(user), uid)
            stale = await session.get(MarketingFeedbackAnalysis, analysis_id)
            assert stale.status is FeedbackAnalysisStatus.DRAFT
            loaded += 1
            if loaded == 2:
                ready.set()
            await ready.wait()
            await FeedbackService(session).review(
                analysis_id, voter, FeedbackAnalysisStatus.ACCEPTED
            )

    await asyncio.gather(accept(), accept())
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(CampaignOptimizationProposal)
            .where(CampaignOptimizationProposal.feedback_analysis_id == analysis_id)
        )
        == 1
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationAction)) == 2
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_api_decision_refresh_and_serialization(db_session, client, decision):
    user, campaign, analysis, proposal = await accepted(db_session, 1)
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    url = f"/api/v1/optimization-actions/{proposal.actions[0].id}/{decision}"
    for _ in range(2):
        response = await client.post(url)
        assert response.status_code == 200
        assert response.json()["status"] == ("APPROVED" if decision == "approve" else "REJECTED")
    detail = (await client.get(f"/api/v1/optimization-proposals/{proposal.id}")).json()
    assert detail["feedback_analysis_id"] == str(analysis.id)
    assert detail["strategy_version"] == 7 and detail["reviewed_by_user_id"] == str(user.id)
    listing = (await client.get(f"/api/v1/campaigns/{campaign.id}/optimization-proposals")).json()
    assert listing[0]["actions"][0]["status"] == detail["status"]


@pytest.mark.parametrize("repaired", [True, False])
async def test_invalid_typed_action_uses_existing_repair_and_never_partial_proposal(
    db_session, monkeypatch, repaired
):
    from app.workers import feedback_worker

    _user, _agent, campaign, _task = await runtime_fixture(db_session)

    class Job:
        id = "mock-feedback-job"

    monkeypatch.setattr(
        feedback_worker.generate_feedback_analysis, "apply_async", lambda **kwargs: Job()
    )
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    analysis = await FeedbackService(db_session).queue_analysis(campaign.id)
    attempts = 0

    async def fake_run(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        action = draft(analysis.input_snapshot)
        if attempts == 1 or not repaired:
            action["target_entity_id"] = str(uuid4())
        result = {
            "summary": "Advisory only",
            "findings": [],
            "recommendations": [recommendation(action)],
            "experiment_ideas": [],
            "limitations": [],
        }
        return type("Result", (), {"final_output": result})()

    class Client:
        async def close(self):
            pass

    monkeypatch.setattr(feedback_worker, "AsyncOpenAI", lambda **kwargs: Client())
    monkeypatch.setattr(feedback_worker, "Runner", type("Runner", (), {"run": fake_run}))
    monkeypatch.setattr(feedback_worker, "Agent", lambda **kwargs: object())
    monkeypatch.setattr(feedback_worker, "OpenAIResponsesModel", lambda *args: object())
    monkeypatch.setattr(feedback_worker, "RunConfig", lambda **kwargs: object())
    monkeypatch.setattr(feedback_worker.settings, "openai_api_key", "fake-test-only")
    if repaired:
        await feedback_worker._run(analysis.agent_run_id)
    else:
        with pytest.raises(RuntimeError, match="REPAIR_EXHAUSTED"):
            await feedback_worker._run(analysis.agent_run_id)
    await db_session.refresh(analysis)
    assert attempts == 2
    assert analysis.status is (
        FeedbackAnalysisStatus.DRAFT if repaired else FeedbackAnalysisStatus.FAILED
    )
    assert len(analysis.recommendations) == (1 if repaired else 0)
    assert (
        await db_session.scalar(select(func.count()).select_from(CampaignOptimizationProposal)) == 0
    )
