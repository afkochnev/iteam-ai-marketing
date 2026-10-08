import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent_run import AgentRun
from app.models.campaign import CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.marketing_experiment import (
    ExperimentMetric,
)
from app.models.marketing_experiment import (
    ExperimentPublicationRole as Role,
)
from app.models.marketing_experiment import (
    ExperimentStatus as Status,
)
from app.models.marketing_experiment import (
    MarketingExperiment as Experiment,
)
from app.models.marketing_experiment import (
    MarketingExperimentPublication as Link,
)
from app.models.optimization import OptimizationActionStatus as AS
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource
from app.models.publication_metrics import PublicationMetricsSnapshot as Snapshot
from app.models.task import Task
from app.models.user import User
from app.schemas.experiment import ExperimentConfiguration, ExperimentCreate
from app.schemas.feedback import FeedbackAnalystResult, OptimizationActionDraft
from app.schemas.optimization import OptimizationActionApplyRequest
from app.services.auth_service import AuthService
from app.services.experiment_service import ExperimentService
from app.services.optimization_apply_service import OptimizationApplyService
from app.services.optimization_proposal_service import OptimizationProposalService
from app.tests.test_optimization import draft, recommendation, snapshot
from app.tests.test_optimization_apply import setup_action

START = datetime(2026, 10, 1, tzinfo=UTC)
MID = START + timedelta(days=3)
END = START + timedelta(days=7)


async def fixture(session):
    user, campaign, article, _, _, analysis, proposal, action = await setup_action(
        session, "EXPERIMENT"
    )
    pubs = []
    for index in range(2):
        post = ContentItem(
            campaign_id=campaign.id,
            source_task_id=article.source_task_id,
            content_type=ContentType.SOCIAL_POST,
            title=f"Observed post {index}",
            status=ContentStatus.APPROVED,
            author_agent_id=article.author_agent_id,
            channel=ContentChannel.TELEGRAM,
        )
        session.add(post)
        await session.flush()
        version = ContentVersion(
            content_item_id=post.id,
            version_number=1,
            content="Existing source",
            structured_content={"text": "Existing source"},
            created_by_agent_id=article.author_agent_id,
        )
        session.add(version)
        await session.flush()
        post.current_version_id = version.id
        publication = Publication(
            campaign_id=campaign.id,
            content_item_id=post.id,
            content_version_id=version.id,
            channel=ContentChannel.TELEGRAM,
            status=PublicationStatus.PUBLISHED,
            published_at=START + timedelta(days=1) if index == 0 else MID + timedelta(days=1),
            external_id=f"offline-{index}",
        )
        session.add(publication)
        await session.flush()
        pubs.append(publication)
    await session.commit()
    config = ExperimentConfiguration(
        baseline_start=START,
        baseline_end=MID,
        experiment_start=MID,
        experiment_end=END,
        baseline_publication_ids=[pubs[0].id],
        experiment_publication_ids=[pubs[1].id],
    )
    return user, campaign, analysis, proposal, action, pubs, config


async def apply(session, user, action, config):
    return await OptimizationProposalService(session).apply_action(
        action.id, user, OptimizationActionApplyRequest(experiment=config)
    )


async def counts(session):
    return [
        await session.scalar(select(func.count()).select_from(model))
        for model in [Task, AgentRun, ContentVersion, Publication, Experiment]
    ]


async def metric(session, publication, observed, value):
    row = Snapshot(
        publication_id=publication.id,
        channel=publication.channel,
        observed_at=observed,
        clicks=value,
        source=MetricsSource.MANUAL,
        metadata_={},
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.parametrize(
    "kind",
    ["CONTENT_REVISION", "PUBLICATION_PLAN_REVISION", "STRATEGY_REVIEW", "NO_CHANGE", "EXPERIMENT"],
)
async def test_structured_spec_rule(kind):
    s = snapshot(uuid4())
    data = draft(s, kind)
    if kind == "EXPERIMENT":
        data.pop("experiment_spec")
    else:
        data["experiment_spec"] = draft(s, "EXPERIMENT")["experiment_spec"]
    with pytest.raises(ValidationError):
        OptimizationActionDraft.model_validate(data)
    with pytest.raises(ValidationError):
        FeedbackAnalystResult.model_validate(
            {
                "summary": "Summary",
                "findings": [],
                "recommendations": [recommendation(data)],
                "experiment_ideas": [],
                "limitations": [],
            }
        )


@pytest.mark.parametrize("metric_name", ["CTR", "ENGAGEMENT_RATE", "ROI", "arbitrary"])
def test_metric_allowlist(metric_name):
    data = draft(snapshot(uuid4()), "EXPERIMENT")
    data["experiment_spec"]["success_metric"] = metric_name
    with pytest.raises(ValidationError):
        OptimizationActionDraft.model_validate(data)


def test_all_metric_and_status_values():
    assert set(ExperimentMetric) == {
        "VIEWS",
        "IMPRESSIONS",
        "REACTIONS",
        "LIKES",
        "COMMENTS",
        "SHARES",
        "CLICKS",
        "SUBSCRIBERS",
    }
    assert set(Status) == {"DRAFT", "APPROVED", "RUNNING", "COMPLETED", "CANCELLED"}


@pytest.mark.parametrize(
    "invalid",
    [
        "naive",
        "reverse_baseline",
        "overlap",
        "reverse_experiment",
        "empty_baseline",
        "empty_experiment",
        "duplicate",
        "both_groups",
    ],
)
def test_configuration_validation(invalid):
    one, two = uuid4(), uuid4()
    data = dict(
        baseline_start=START,
        baseline_end=MID,
        experiment_start=MID,
        experiment_end=END,
        baseline_publication_ids=[one],
        experiment_publication_ids=[two],
    )
    if invalid == "naive":
        data["baseline_start"] = START.replace(tzinfo=None)
    if invalid == "reverse_baseline":
        data["baseline_start"] = MID
    if invalid == "overlap":
        data["experiment_start"] = MID - timedelta(seconds=1)
    if invalid == "reverse_experiment":
        data["experiment_end"] = MID
    if invalid == "empty_baseline":
        data["baseline_publication_ids"] = []
    if invalid == "empty_experiment":
        data["experiment_publication_ids"] = []
    if invalid == "duplicate":
        data["baseline_publication_ids"] = [one, one]
    if invalid == "both_groups":
        data["experiment_publication_ids"] = [one]
    with pytest.raises(ValidationError):
        ExperimentConfiguration.model_validate(data)


async def test_create_is_draft_durable_no_business_mutations(db_session):
    user, campaign, analysis, proposal, action, pubs, config = await fixture(db_session)
    before = await counts(db_session)
    old = [(p.id, p.content_version_id, p.status, p.published_at) for p in pubs]
    assert action.experiment_spec["success_metric"] == "CLICKS"
    observed = []

    def flush(session, context, instances):
        if action.status is AS.APPLIED:
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM marketing_experiments "
                        "WHERE source_optimization_action_id=:id"
                    ),
                    {"id": action.id},
                )
                == 1
            )
            observed.append(True)

    event.listen(db_session.sync_session, "before_flush", flush)
    try:
        result = await apply(db_session, user, action, config)
    finally:
        event.remove(db_session.sync_session, "before_flush", flush)
    assert observed and result.artifact_type == "MARKETING_EXPERIMENT"
    assert result.task_id is result.agent_run_id is result.publication_plan_id is None
    row = await db_session.get(Experiment, result.experiment_id)
    assert row.status is Status.DRAFT and row.hypothesis == action.experiment_spec["hypothesis"]
    assert action.status is AS.APPLIED and proposal.status.value == "APPLIED"
    after = await counts(db_session)
    assert before[:-1] == after[:-1] and after[-1] == 1
    assert old == [(p.id, p.content_version_id, p.status, p.published_at) for p in pubs]
    response = await ExperimentService(db_session).response(row)
    assert response.proposal_id == proposal.id and response.feedback_analysis_id == analysis.id
    assert {link.content_version_id for link in response.publications} == {
        p.content_version_id for p in pubs
    }
    for name in ["OPTIMIZATION_ACTION_APPLIED", "MARKETING_EXPERIMENT_CREATED"]:
        logs = list(
            await db_session.scalars(select(ActivityLog).where(ActivityLog.event_type == name))
        )
        assert len(logs) == 1 and logs[0].user_id == user.id
        metadata = logs[0].metadata_
        assert metadata["action_id"] == str(action.id) and metadata["proposal_id"] == str(
            proposal.id
        )
        assert metadata["feedback_analysis_id"] == str(analysis.id)
        assert (
            "hypothesis" not in metadata
            and "prompt" not in metadata
            and "human_comment" not in metadata
        )


async def test_repeat_and_two_entrypoints_share_same_artifact(db_session):
    user, campaign, _, _, action, _, config = await fixture(db_session)
    first = await apply(db_session, user, action, config)
    repeat = await OptimizationApplyService(db_session).apply(
        action.id, user, OptimizationActionApplyRequest()
    )
    via_campaign = await ExperimentService(db_session).create_from_action(
        campaign.id,
        user,
        ExperimentCreate(source_optimization_action_id=action.id, **config.model_dump()),
    )
    assert first.artifact_id == repeat.artifact_id == via_campaign.id
    assert await db_session.scalar(select(func.count()).select_from(Experiment)) == 1


async def test_concurrent_create_both_entrypoints(db_session):
    user, campaign, _, _, action, _, config = await fixture(db_session)
    uid, cid, aid = user.id, campaign.id, action.id

    async def create(use_campaign):
        async with async_session_factory() as session:
            current = await session.get(User, uid)
            if use_campaign:
                return (
                    await ExperimentService(session).create_from_action(
                        cid,
                        current,
                        ExperimentCreate(source_optimization_action_id=aid, **config.model_dump()),
                    )
                ).id
            return (
                await OptimizationApplyService(session).apply(
                    aid, current, OptimizationActionApplyRequest(experiment=config)
                )
            ).artifact_id

    ids = await asyncio.gather(create(False), create(True), create(False))
    assert (
        len(set(ids)) == 1
        and await db_session.scalar(select(func.count()).select_from(Experiment)) == 1
    )


async def test_legacy_readable_but_not_executable(db_session, client):
    user, _, analysis, proposal, action, _, config = await fixture(db_session)
    legacy = deepcopy(analysis.recommendations)
    legacy[0]["proposed_action"].pop("experiment_spec", None)
    analysis.recommendations = legacy
    action.experiment_spec = None
    await db_session.commit()
    cookie = AuthService.create_access_token(user)
    client.cookies.set("access_token", cookie)
    from app.core.config import settings

    client.cookies.set(settings.auth_cookie_name, cookie)
    response = await client.get(f"/api/v1/optimization-proposals/{proposal.id}")
    assert response.status_code == 200 and response.json()["actions"][0]["experiment_spec"] is None
    with pytest.raises(AppError) as error:
        await apply(db_session, user, action, config)
    assert error.value.code == "OPTIMIZATION_EXPERIMENT_SPEC_MISSING"
    assert analysis.recommendations == legacy and action.status is AS.APPROVED
    assert await db_session.scalar(select(func.count()).select_from(Experiment)) == 0


@pytest.mark.parametrize(
    "invalid,code",
    [
        ("proposed", "OPTIMIZATION_ACTION_NOT_APPROVED"),
        ("archived", "CAMPAIGN_ARCHIVED"),
        ("stale", "OPTIMIZATION_CONTEXT_STALE"),
        ("evidence", "OPTIMIZATION_ACTION_INVALID"),
        ("foreign_publication", "EXPERIMENT_PUBLICATIONS_INVALID"),
        ("absent_publication", "EXPERIMENT_PUBLICATIONS_INVALID"),
    ],
)
async def test_create_preconditions(db_session, invalid, code):
    user, campaign, _, _, action, pubs, config = await fixture(db_session)
    if invalid == "proposed":
        action.status = AS.PROPOSED
    if invalid == "archived":
        campaign.status = CampaignStatus.ARCHIVED
    if invalid == "stale":
        campaign.strategy_version += 1
    if invalid == "evidence":
        action.evidence_refs = [{"type": "publication", "id": str(uuid4())}]
    if invalid == "foreign_publication":
        from app.models.campaign import Campaign

        other = Campaign(
            name="Other", goal="Other", status=CampaignStatus.ACTIVE, created_by=user.id
        )
        db_session.add(other)
        await db_session.flush()
        pubs[0].campaign_id = other.id
    if invalid == "absent_publication":
        config.baseline_publication_ids = [uuid4()]
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await apply(db_session, user, action, config)
    assert error.value.code == code
    assert await db_session.scalar(select(func.count()).select_from(Experiment)) == 0


@pytest.mark.parametrize("status", [Status.DRAFT, Status.APPROVED, Status.RUNNING])
async def test_cancel_is_idempotent_without_publication_changes(db_session, status):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    service = ExperimentService(db_session)
    if status is not Status.DRAFT:
        await service.approve(result.artifact_id, user)
    if status is Status.RUNNING:
        await service.advance(now=MID)
    first = await service.cancel(result.artifact_id, user)
    repeated = await service.cancel(result.artifact_id, user)
    assert first.status is repeated.status is Status.CANCELLED
    assert first.cancelled_by_user_id == user.id
    assert (
        all(p.status is PublicationStatus.PUBLISHED for p in pubs) and action.status is AS.APPLIED
    )
    with pytest.raises(AppError):
        await service.approve(result.artifact_id, user)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.event_type == "MARKETING_EXPERIMENT_CANCELLED")
        )
        == 1
    )


async def test_separate_approval_and_lifecycle_idempotent(db_session):
    user, campaign, _, _, action, _, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    service = ExperimentService(db_session)
    assert await service.advance(now=END) == []
    first = await service.approve(result.artifact_id, user)
    again = await service.approve(result.artifact_id, user)
    assert first.status is Status.APPROVED and first.approved_at == again.approved_at
    assert await service.advance(now=MID - timedelta(seconds=1)) == []
    assert await service.advance(now=MID) == [result.artifact_id]
    assert (await service.get(result.artifact_id)).status is Status.RUNNING
    assert await service.advance(now=MID) == []
    assert await service.advance(now=END) == [result.artifact_id]
    row = await service.get(result.artifact_id)
    assert row.status is Status.COMPLETED and "недостаточно" in row.result_summary
    assert await service.advance(now=END) == []
    assert (await service.approve(row.id, user)).status is Status.COMPLETED
    with pytest.raises(AppError) as error:
        await service.cancel(row.id, user)
    assert error.value.code == "EXPERIMENT_ALREADY_COMPLETED"
    for name in [
        "MARKETING_EXPERIMENT_APPROVED",
        "MARKETING_EXPERIMENT_STARTED",
        "MARKETING_EXPERIMENT_COMPLETED",
    ]:
        assert (
            await db_session.scalar(
                select(func.count()).select_from(ActivityLog).where(ActivityLog.event_type == name)
            )
            == 1
        )


@pytest.mark.parametrize("b,e", [(12, 15), (0, 5), (None, 5), (12, None)])
async def test_deterministic_latest_snapshot_null_and_zero(db_session, b, e):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    old = await metric(db_session, pubs[0], MID - timedelta(hours=2), 999)
    baseline = await metric(db_session, pubs[0], MID, b)
    future = await metric(db_session, pubs[0], MID + timedelta(seconds=1), 999)
    experiment = await metric(db_session, pubs[1], END, e)
    service = ExperimentService(db_session)
    await service.approve(result.artifact_id, user)
    await service.advance(now=END + timedelta(days=1))
    row = await service.get(result.artifact_id)
    data = row.result_data
    assert data["groups"]["BASELINE"]["mean"] == b and data["groups"]["EXPERIMENT"]["mean"] == e
    assert set(data["used_snapshot_ids"]) == {str(baseline.id), str(experiment.id)}
    assert (
        str(old.id) not in data["used_snapshot_ids"]
        and str(future.id) not in data["used_snapshot_ids"]
    )
    if b is None or e is None:
        assert data["absolute_delta"] is None and "недостаточно" in row.result_summary
        assert any("неполное покрытие" in item for item in row.limitations)
    elif b == 0:
        assert data["absolute_delta"] == 5 and data["percent_delta"] is None
        assert any("Нулевое" in item for item in row.limitations)
    else:
        assert data["absolute_delta"] == 3 and data["percent_delta"] == 25
    assert (
        "не доказательство причинного эффекта" in row.result_summary
        or "недостаточно" in row.result_summary
    )
    assert not any(
        phrase in row.result_summary for phrase in ["доказал", "статистически значим", "победитель"]
    )


async def test_unpublished_and_window_limitations(db_session):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    pubs[0].published_at = START - timedelta(days=1)
    pubs[1].status = PublicationStatus.DRAFT
    pubs[1].published_at = None
    await db_session.commit()
    result = await apply(db_session, user, action, config)
    await metric(db_session, pubs[0], MID, 10)
    await metric(db_session, pubs[1], END, 500)
    service = ExperimentService(db_session)
    await service.approve(result.artifact_id, user)
    await service.advance(now=END)
    row = await service.get(result.artifact_id)
    assert row.result_data["groups"]["EXPERIMENT"]["mean"] is None
    assert any("вне ожидаемого окна" in item for item in row.limitations)


@pytest.mark.parametrize("operation", ["approve", "cancel"])
async def test_archived_history_readable_mutations_blocked(db_session, operation):
    user, campaign, _, _, action, _, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    service = ExperimentService(db_session)
    assert len(await service.list_for_campaign(campaign.id)) == 1
    with pytest.raises(AppError) as error:
        await getattr(service, operation)(result.artifact_id, user)
    assert error.value.code == "CAMPAIGN_ARCHIVED"
    assert await service.advance(now=END) == []


async def test_approval_revalidates_publication_campaign(db_session):
    user, campaign, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    from app.models.campaign import Campaign

    other = Campaign(name="Other", goal="Other", status=CampaignStatus.ACTIVE, created_by=user.id)
    db_session.add(other)
    await db_session.flush()
    pubs[1].campaign_id = other.id
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await ExperimentService(db_session).approve(result.artifact_id, user)
    assert error.value.code == "EXPERIMENT_PUBLICATIONS_INVALID"


async def test_unique_action_membership_and_restrict(db_session):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    row = await db_session.get(Experiment, result.artifact_id)
    values = {
        column.name: getattr(row, column.name)
        for column in Experiment.__table__.columns
        if column.name not in ["id", "created_at", "updated_at"]
    }
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Experiment(**values))
            await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                Link(
                    experiment_id=row.id,
                    publication_id=pubs[0].id,
                    content_version_id=pubs[0].content_version_id,
                    role=Role.EXPERIMENT,
                )
            )
            await db_session.flush()
    for statement in [
        delete(Experiment).where(Experiment.id == row.id),
        delete(Publication).where(Publication.id == pubs[0].id),
    ]:
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(statement)


async def test_api_auth_create_get_approve_cancel(client, db_session):
    user, campaign, _, _, action, _, config = await fixture(db_session)
    assert (await client.get(f"/api/v1/campaigns/{campaign.id}/experiments")).status_code == 401
    from app.core.config import settings

    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    body = {"source_optimization_action_id": str(action.id), **config.model_dump(mode="json")}
    response = await client.post(f"/api/v1/campaigns/{campaign.id}/experiments", json=body)
    assert response.status_code == 200
    eid = response.json()["id"]
    assert response.json()["status"] == "DRAFT"
    assert (await client.get(f"/api/v1/experiments/{eid}")).status_code == 200
    assert len((await client.get(f"/api/v1/campaigns/{campaign.id}/experiments")).json()) == 1
    assert (await client.post(f"/api/v1/experiments/{eid}/approve")).json()["status"] == "APPROVED"
    assert (await client.post(f"/api/v1/experiments/{eid}/cancel")).json()["status"] == "CANCELLED"
    body.pop("source_optimization_action_id")
    assert (
        await client.post(f"/api/v1/campaigns/{campaign.id}/experiments", json=body)
    ).status_code == 422


async def test_partial_coverage_unequal_groups_and_low_observation_warning(db_session):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    extra = Publication(
        campaign_id=pubs[0].campaign_id,
        content_item_id=pubs[0].content_item_id,
        content_version_id=pubs[0].content_version_id,
        channel=pubs[0].channel,
        status=PublicationStatus.PUBLISHED,
        published_at=START + timedelta(days=1),
    )
    db_session.add(extra)
    await db_session.commit()
    config.baseline_publication_ids.append(extra.id)
    result = await apply(db_session, user, action, config)
    await metric(db_session, pubs[0], MID, 10)
    await metric(db_session, extra, MID, None)
    await metric(db_session, pubs[1], END, 20)
    service = ExperimentService(db_session)
    await service.approve(result.artifact_id, user)
    await service.advance(now=END)
    row = await service.get(result.artifact_id)
    baseline = row.result_data["groups"]["BASELINE"]
    assert baseline["linked_publication_count"] == 2 and baseline["observed_publication_count"] == 2
    assert (
        baseline["sample_count"] == 1
        and baseline["metric_coverage"] == 0.5
        and baseline["mean"] == 10
    )
    assert any("Неравные размеры" in item for item in row.limitations)
    assert any("малое число" in item for item in row.limitations)


async def test_equal_observed_times_have_stable_id_tiebreaker(db_session):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    first = await metric(db_session, pubs[0], MID, 10)
    second = await metric(db_session, pubs[0], MID, 20)
    first.created_at = second.created_at
    await db_session.commit()
    expected = max([first, second], key=lambda row: row.id)
    service = ExperimentService(db_session)
    await service.approve(result.artifact_id, user)
    await service.advance(now=END)
    row = await service.get(result.artifact_id)
    assert row.result_data["groups"]["BASELINE"]["observations"][0]["snapshot_id"] == str(
        expected.id
    )


async def test_concurrent_lifecycle_completes_once(db_session):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    await metric(db_session, pubs[0], MID, 10)
    await metric(db_session, pubs[1], END, 20)
    await ExperimentService(db_session).approve(result.artifact_id, user)

    async def advance():
        async with async_session_factory() as session:
            return await ExperimentService(session).advance(now=END)

    first, second = await asyncio.gather(advance(), advance())
    assert len(first) + len(second) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.event_type == "MARKETING_EXPERIMENT_COMPLETED")
        )
        == 1
    )


async def test_fk_restrict_and_new_schema(db_session):
    rows = list(
        await db_session.execute(
            text(
                "SELECT c.confdeltype::text FROM pg_constraint c "
                "JOIN pg_class t ON t.oid=c.conrelid "
                "WHERE c.contype='f' AND t.relname IN "
                "('marketing_experiments','marketing_experiment_publications')"
            )
        )
    )
    assert len(rows) == 8 and all(row[0] == "r" for row in rows)
    assert (
        await db_session.scalar(text("SELECT version_num FROM alembic_version")) == "20261008_0025"
    )


async def test_approval_rechecks_exact_content_version(db_session):
    user, _, _, _, action, pubs, config = await fixture(db_session)
    result = await apply(db_session, user, action, config)
    old_version = pubs[0].content_version_id
    new = ContentVersion(
        content_item_id=pubs[0].content_item_id,
        version_number=2,
        content="A separately replaced source",
        structured_content={},
        created_by_agent_id=None,
    )
    db_session.add(new)
    await db_session.flush()
    pubs[0].content_version_id = new.id
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await ExperimentService(db_session).approve(result.artifact_id, user)
    assert error.value.code == "EXPERIMENT_SOURCE_CHANGED"
    links = list(
        await db_session.scalars(select(Link).where(Link.experiment_id == result.artifact_id))
    )
    assert any(link.content_version_id == old_version for link in links)


async def test_legacy_analysis_acceptance_keeps_raw_json(db_session):
    from app.models.marketing_feedback import FeedbackAnalysisStatus
    from app.services.feedback_service import FeedbackService
    from app.tests.test_optimization import typed_analysis

    user, _, analysis = await typed_analysis(db_session, count=1)
    recommendation_data = deepcopy(analysis.recommendations[0])
    action_data = draft(analysis.input_snapshot, "EXPERIMENT")
    action_data.pop("experiment_spec")
    recommendation_data["proposed_action"] = action_data
    analysis.recommendations = [recommendation_data]
    original = deepcopy(analysis.recommendations)
    await db_session.commit()
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    from app.models.optimization import CampaignOptimizationAction, CampaignOptimizationProposal

    proposal = await db_session.scalar(
        select(CampaignOptimizationProposal).where(
            CampaignOptimizationProposal.feedback_analysis_id == analysis.id
        )
    )
    action = await db_session.scalar(
        select(CampaignOptimizationAction).where(
            CampaignOptimizationAction.proposal_id == proposal.id
        )
    )
    assert action.experiment_spec is None and analysis.recommendations == original


async def test_experiment_config_rejected_for_other_action(db_session):
    user, _, _, _, action, _, config = await fixture(db_session)
    from app.models.optimization import OptimizationActionType

    action.type = OptimizationActionType.NO_CHANGE
    action.experiment_spec = None
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await apply(db_session, user, action, config)
    assert error.value.code == "EXPERIMENT_CONFIG_NOT_APPLICABLE"
