import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tasks import run_task
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.marketing_feedback import FeedbackAnalysisStatus
from app.models.task import Task, TaskType
from app.schemas.agent_run import AgentRunContextRequest
from app.schemas.feedback import FeedbackAnalystResult
from app.schemas.publication import PublicationCreate
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.feedback_service import FeedbackService
from app.services.publication_service import PublicationService
from app.services.task_service import TaskService
from app.tests.test_publications import _approved_post


@pytest.mark.integration
async def test_human_feedback_binds_exact_publication_version(
    db_session: AsyncSession,
) -> None:
    user, campaign, post, version = await _approved_post(db_session)
    publication = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id,
            content_version_id=version.id,
            channel=post.channel,
        ),
        user,
    )
    row = await FeedbackService(db_session).create_feedback(
        campaign.id,
        user,
        {
            "publication_id": publication.id,
            "category": "TONE",
            "rating": 3,
            "comment": "Слишком академично",
        },
    )
    assert row.content_version_id == version.id
    assert row.publication_id == publication.id
    assert row.comment == "Слишком академично"


@pytest.mark.integration
async def test_feedback_rejects_cross_campaign_reference(
    db_session: AsyncSession,
) -> None:
    user, campaign, post, version = await _approved_post(db_session)
    publication = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id,
            content_version_id=version.id,
            channel=post.channel,
        ),
        user,
    )
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).create_feedback(
            uuid4(),
            user,
            {"publication_id": publication.id, "category": "OTHER", "comment": "x"},
        )
    assert error.value.code == "FEEDBACK_REFERENCE_INVALID"


@pytest.mark.integration
async def test_analysis_freezes_input_and_review_is_terminal(
    db_session: AsyncSession,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    await FeedbackService(db_session).create_feedback(
        campaign.id,
        user,
        {"category": "ENGAGEMENT", "comment": "Практический заход работает лучше."},
    )
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    assert analysis.status is FeedbackAnalysisStatus.DRAFT
    assert len(analysis.input_snapshot["feedback_ids"]) == 1
    events = list(
        (
            await db_session.scalars(
                select(ActivityLog.event_type).where(ActivityLog.campaign_id == campaign.id)
            )
        ).all()
    )
    assert {
        "FEEDBACK_ANALYSIS_QUEUED",
        "FEEDBACK_ANALYSIS_STARTED",
        "FEEDBACK_ANALYSIS_COMPLETED",
    }.issubset(set(events))
    accepted = await FeedbackService(db_session).review(
        analysis.id, user, FeedbackAnalysisStatus.ACCEPTED
    )
    assert accepted.status is FeedbackAnalysisStatus.ACCEPTED
    again = await FeedbackService(db_session).review(
        analysis.id, user, FeedbackAnalysisStatus.ACCEPTED
    )
    assert again.id == accepted.id
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.REJECTED)
    assert error.value.code == "FEEDBACK_ANALYSIS_ALREADY_REVIEWED"


@pytest.mark.integration
async def test_analysis_queue_creates_agent_run_and_freezes_snapshot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)

    class Result:
        id = "feedback-test-job"

    monkeypatch.setattr(
        "app.workers.feedback_worker.generate_feedback_analysis.delay",
        lambda _run_id: Result(),
    )
    analysis = await FeedbackService(db_session).queue_analysis(campaign.id)
    run = await db_session.get(AgentRun, analysis.agent_run_id)
    assert run is not None
    assert run.status is AgentRunStatus.QUEUED
    assert run.input_data["feedback_snapshot"]["campaign_id"] == str(campaign.id)
    assert analysis.generated_at is None


@pytest.mark.integration
async def test_feedback_evidence_validation_rejects_unknown_and_accepts_frozen_reference(
    db_session: AsyncSession,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    feedback = await FeedbackService(db_session).create_feedback(
        campaign.id,
        user,
        {"category": "TONE", "comment": "Тон стоит сделать теплее."},
    )
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    valid = FeedbackAnalystResult.model_validate(
        {
            "summary": "Наблюдение",
            "findings": [
                {
                    "type": "EDITORIAL",
                    "observation": "Обратная связь зафиксирована.",
                    "evidence_refs": [{"type": "marketing_feedback", "id": str(feedback.id)}],
                    "confidence": "provided",
                }
            ],
            "recommendations": [],
            "experiment_ideas": [],
            "limitations": [],
        }
    )
    FeedbackService.validate_evidence(analysis.input_snapshot, valid)
    invalid = valid.model_copy(deep=True)
    invalid.findings[0].evidence_refs[0].id = uuid4()
    with pytest.raises(AppError) as error:
        FeedbackService.validate_evidence(analysis.input_snapshot, invalid)
    assert error.value.code == "FEEDBACK_EVIDENCE_INVALID"


@pytest.mark.integration
async def test_accepted_analysis_context_requires_same_campaign_and_terminal_review(
    db_session: AsyncSession,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).accepted_snapshot(analysis.id, campaign.id)
    assert error.value.code == "FEEDBACK_ANALYSIS_NOT_ACCEPTED"
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    accepted = await FeedbackService(db_session).accepted_snapshot(analysis.id, campaign.id)
    assert accepted["analysis_id"] == str(analysis.id)
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).accepted_snapshot(analysis.id, uuid4())
    assert error.value.code == "FEEDBACK_ANALYSIS_INVALID"


async def _director_task(db_session: AsyncSession, campaign_id: object) -> tuple[object, object]:
    director = await db_session.scalar(select(Agent).where(Agent.slug == "marketing_director"))
    if director is None:
        director = Agent(
            name="Marketing Director",
            slug="marketing_director",
            role="marketing_director",
            system_prompt="Plan campaigns.",
            model="test-model",
            status=AgentStatus.ACTIVE,
            autonomy_level=2,
            settings={},
        )
        db_session.add(director)
        await db_session.flush()
    task = await TaskService(db_session).create_task(
        TaskCreate(
            campaign_id=campaign_id,
            task_type=TaskType.CAMPAIGN_PLANNING,
            title="Plan campaign",
            assigned_agent_id=director.id,
        )
    )
    return task, director


@pytest.mark.integration
async def test_director_run_path_binds_exact_accepted_analysis_and_supports_no_analysis(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    task, _director = await _director_task(db_session, campaign.id)

    async def no_enqueue(
        _service: AgentRunService, run: AgentRun, *, countdown: int = 0
    ) -> AgentRun:
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", no_enqueue)
    run_summary = await run_task(
        task.id,
        user,
        db_session,
        AgentRunContextRequest(feedback_analysis_id=analysis.id),
    )
    run = await db_session.get(AgentRun, run_summary.id)
    assert run is not None
    assert run.input_data["feedback_analysis_snapshot"]["analysis_id"] == str(analysis.id)
    events = list(
        (
            await db_session.scalars(
                select(ActivityLog.event_type).where(ActivityLog.task_id == task.id)
            )
        ).all()
    )
    assert "FEEDBACK_ANALYSIS_USED" in events

    no_context_task, _director = await _director_task(db_session, campaign.id)
    no_context = await run_task(no_context_task.id, user, db_session, None)
    no_context_run = await db_session.get(AgentRun, no_context.id)
    assert no_context_run is not None
    assert "feedback_analysis_snapshot" not in no_context_run.input_data


@pytest.mark.integration
async def test_smm_run_path_binds_exact_accepted_analysis(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, campaign, post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    task = await db_session.get(Task, post.source_task_id)
    assert task is not None

    async def no_enqueue(
        _service: AgentRunService, run: AgentRun, *, countdown: int = 0
    ) -> AgentRun:
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", no_enqueue)
    run_summary = await run_task(
        task.id,
        user,
        db_session,
        AgentRunContextRequest(feedback_analysis_id=analysis.id),
    )
    run = await db_session.get(AgentRun, run_summary.id)
    assert run is not None
    assert run.input_data["feedback_analysis_snapshot"]["analysis_id"] == str(analysis.id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "status",
    [
        FeedbackAnalysisStatus.DRAFT,
        FeedbackAnalysisStatus.FAILED,
        FeedbackAnalysisStatus.REJECTED,
    ],
)
async def test_run_path_rejects_non_accepted_feedback_analysis_statuses(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    status: FeedbackAnalysisStatus,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    analysis.status = status
    await db_session.commit()
    task, _director = await _director_task(db_session, campaign.id)

    async def no_enqueue(
        _service: AgentRunService, run: AgentRun, *, countdown: int = 0
    ) -> AgentRun:
        return run

    monkeypatch.setattr(AgentRunService, "enqueue", no_enqueue)
    with pytest.raises(AppError) as error:
        await run_task(
            task.id,
            user,
            db_session,
            AgentRunContextRequest(feedback_analysis_id=analysis.id),
        )
    assert error.value.code == "FEEDBACK_ANALYSIS_NOT_ACCEPTED"


@pytest.mark.integration
async def test_accepting_analysis_does_not_create_downstream_work(
    db_session: AsyncSession,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    before = len(list((await db_session.scalars(select(AgentRun))).all()))
    await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    after = len(list((await db_session.scalars(select(AgentRun))).all()))
    assert after == before


@pytest.mark.integration
async def test_double_reject_is_idempotent_and_accept_conflicts(
    db_session: AsyncSession,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    rejected = await FeedbackService(db_session).review(
        analysis.id, user, FeedbackAnalysisStatus.REJECTED
    )
    again = await FeedbackService(db_session).review(
        analysis.id, user, FeedbackAnalysisStatus.REJECTED
    )
    assert again.id == rejected.id
    with pytest.raises(AppError) as error:
        await FeedbackService(db_session).review(analysis.id, user, FeedbackAnalysisStatus.ACCEPTED)
    assert error.value.code == "FEEDBACK_ANALYSIS_ALREADY_REVIEWED"


@pytest.mark.integration
async def test_postgres_accept_reject_race_has_one_terminal_winner(
    db_session: AsyncSession,
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    analysis = await FeedbackService(db_session).generate_analysis(campaign.id)
    analysis_id = analysis.id
    user_id = user.id

    async def decide(status: FeedbackAnalysisStatus) -> object:
        async with async_session_factory() as session:
            locked_user = await session.get(type(user), user_id)
            assert locked_user is not None
            try:
                return await FeedbackService(session).review(analysis_id, locked_user, status)
            except AppError as error:
                return error

    accepted, rejected = await asyncio.gather(
        decide(FeedbackAnalysisStatus.ACCEPTED),
        decide(FeedbackAnalysisStatus.REJECTED),
    )
    outcomes = [accepted, rejected]
    assert sum(not isinstance(item, AppError) for item in outcomes) == 1
    assert sum(isinstance(item, AppError) for item in outcomes) == 1
    conflict = next(item for item in outcomes if isinstance(item, AppError))
    assert conflict.code == "FEEDBACK_ANALYSIS_ALREADY_REVIEWED"


@pytest.mark.integration
async def test_duplicate_feedback_worker_execution_is_noop_after_completion(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, campaign, _post, _version = await _approved_post(db_session)

    class Job:
        id = "feedback-duplicate-job"

    monkeypatch.setattr(
        "app.workers.feedback_worker.generate_feedback_analysis.delay",
        lambda _run_id: Job(),
    )
    analysis = await FeedbackService(db_session).queue_analysis(campaign.id)
    run = await db_session.get(AgentRun, analysis.agent_run_id)
    assert run is not None
    run.status = AgentRunStatus.COMPLETED
    await db_session.commit()

    from app.workers import feedback_worker

    async def should_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("duplicate worker must not invoke the model")

    monkeypatch.setattr(feedback_worker.Runner, "run", should_not_run)
    await feedback_worker._run(analysis.agent_run_id)
    refreshed = await db_session.get(AgentRun, analysis.agent_run_id)
    assert refreshed is not None and refreshed.status is AgentRunStatus.COMPLETED


@pytest.mark.integration
async def test_feedback_analysis_repair_uses_same_frozen_snapshot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, campaign, _post, _version = await _approved_post(db_session)
    feedback = await FeedbackService(db_session).create_feedback(
        campaign.id,
        user,
        {"category": "TONE", "comment": "Нужен более живой тон."},
    )

    class Job:
        id = "feedback-repair-job"

    monkeypatch.setattr(
        "app.workers.feedback_worker.generate_feedback_analysis.delay",
        lambda _run_id: Job(),
    )
    analysis = await FeedbackService(db_session).queue_analysis(campaign.id)
    snapshot_ids = list(analysis.input_snapshot["feedback_ids"])

    from app.workers import feedback_worker

    class FakeClient:
        async def close(self) -> None:
            return None

    class FakeResult:
        def __init__(self, value: dict[str, object]):
            self.final_output = value

    attempts = 0

    async def fake_run(*_args: object, **_kwargs: object) -> FakeResult:
        nonlocal attempts
        attempts += 1
        evidence_id = str(uuid4()) if attempts == 1 else snapshot_ids[0]
        return FakeResult(
            {
                "summary": "Исправленный анализ",
                "findings": [
                    {
                        "type": "EDITORIAL",
                        "observation": "Наблюдение.",
                        "evidence_refs": [{"type": "marketing_feedback", "id": evidence_id}],
                        "confidence": "provided",
                    }
                ],
                "recommendations": [],
                "experiment_ideas": [],
                "limitations": [],
            }
        )

    monkeypatch.setattr(feedback_worker, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(feedback_worker, "Runner", type("RunnerStub", (), {"run": fake_run}))
    monkeypatch.setattr(feedback_worker, "Agent", lambda **_kwargs: object())
    monkeypatch.setattr(feedback_worker, "OpenAIResponsesModel", lambda *_args: object())
    monkeypatch.setattr(feedback_worker, "RunConfig", lambda **_kwargs: object())
    monkeypatch.setattr(feedback_worker.settings, "openai_api_key", "test-key")

    await feedback_worker._run(analysis.agent_run_id)
    await db_session.refresh(analysis)
    run = await db_session.get(AgentRun, analysis.agent_run_id)
    assert attempts == 2
    assert run is not None and run.status is AgentRunStatus.COMPLETED
    assert run.output_data is not None
    assert run.output_data["repair_attempt_count"] == 1
    assert analysis.status is FeedbackAnalysisStatus.DRAFT
    assert analysis.input_snapshot["feedback_ids"] == snapshot_ids
    assert str(feedback.id) in snapshot_ids


@pytest.mark.integration
async def test_feedback_analysis_repair_exhaustion_fails_without_completed_result(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user, campaign, _post, _version = await _approved_post(db_session)

    class Job:
        id = "feedback-repair-exhausted-job"

    monkeypatch.setattr(
        "app.workers.feedback_worker.generate_feedback_analysis.delay",
        lambda _run_id: Job(),
    )
    analysis = await FeedbackService(db_session).queue_analysis(campaign.id)

    from app.workers import feedback_worker

    class FakeClient:
        async def close(self) -> None:
            return None

    class FakeResult:
        final_output = {
            "summary": "Невалидный анализ",
            "findings": [
                {
                    "type": "EDITORIAL",
                    "observation": "Наблюдение.",
                    "evidence_refs": [{"type": "publication", "id": str(uuid4())}],
                    "confidence": "provided",
                }
            ],
            "recommendations": [],
            "experiment_ideas": [],
            "limitations": [],
        }

    async def fake_run(*_args: object, **_kwargs: object) -> FakeResult:
        return FakeResult()

    monkeypatch.setattr(feedback_worker, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(feedback_worker, "Runner", type("RunnerStub", (), {"run": fake_run}))
    monkeypatch.setattr(feedback_worker, "Agent", lambda **_kwargs: object())
    monkeypatch.setattr(feedback_worker, "OpenAIResponsesModel", lambda *_args: object())
    monkeypatch.setattr(feedback_worker, "RunConfig", lambda **_kwargs: object())
    monkeypatch.setattr(feedback_worker.settings, "openai_api_key", "test-key")

    with pytest.raises(RuntimeError, match="FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED"):
        await feedback_worker._run(analysis.agent_run_id)
    await db_session.refresh(analysis)
    run = await db_session.get(AgentRun, analysis.agent_run_id)
    assert run is not None and run.status is AgentRunStatus.FAILED
    assert run.error_code == "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED"
    assert run.output_data is not None
    assert run.output_data["repair_attempt_count"] == 1
    assert analysis.status is FeedbackAnalysisStatus.FAILED
