from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import TaskStatus
from app.models.user import User
from app.schemas.publication_plan import (
    PlanItemInput,
    PublicationPlanAgentResult,
    PublicationPlanCreate,
    PublicationPlanGenerateRequest,
)
from app.services.publication_plan_service import PublicationPlanService
from app.services.publication_planner_digest import (
    build_article_planning_digest,
    claim_ids_for_version,
)
from app.tests.test_smm_integration import smm_fixture
from app.workers import publication_plan_worker
from app.workers.publication_plan_worker import (
    _build_repair_prompt,
    _editorial_validation_errors,
)


def test_publication_plan_horizon_is_timezone_aware_and_bounded() -> None:
    payload = PublicationPlanCreate(
        planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
        planning_horizon_end=datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert payload.planning_horizon_end > payload.planning_horizon_start
    with pytest.raises(ValidationError):
        PublicationPlanCreate(
            planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
            planning_horizon_end=datetime(2027, 1, 1, tzinfo=UTC),
        )


def test_plan_item_requires_timezone_aware_schedule() -> None:
    with pytest.raises(ValidationError):
        PublicationPlanCreate(
            planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
            planning_horizon_end=datetime(2026, 10, 2, tzinfo=UTC),
            items=[
                {
                    "scheduled_at": datetime(2026, 10, 1),
                    "channel": ContentChannel.TELEGRAM,
                    "source_content_version_id": uuid4(),
                    "topic": "Тема",
                    "angle": "Угол",
                    "purpose": "Цель",
                    "format": "observation",
                    "message_brief": "Краткий бриф",
                }
            ],
        )


def test_plan_generation_bounds_channels_and_item_count() -> None:
    request = PublicationPlanGenerateRequest(
        planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
        planning_horizon_end=datetime(2026, 10, 14, tzinfo=UTC),
        channels=[ContentChannel.TELEGRAM, ContentChannel.VK],
        total_items=6,
    )
    assert request.total_items == 6
    assert set(request.channels) == {ContentChannel.TELEGRAM, ContentChannel.VK}


def test_plan_generation_rejects_excessive_item_count() -> None:
    with pytest.raises(ValidationError):
        PublicationPlanGenerateRequest(
            planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
            planning_horizon_end=datetime(2026, 10, 14, tzinfo=UTC),
            total_items=101,
        )


def test_planner_repair_prompt_reuses_frozen_contract_without_secrets() -> None:
    snapshot = {
        "article_version_ids": ["allowed-version"],
        "channels": ["TELEGRAM", "VK"],
        "planning_horizon_start": "2026-10-01T00:00:00+00:00",
        "planning_horizon_end": "2026-10-14T00:00:00+00:00",
        "total_items": 6,
    }
    prompt = _build_repair_prompt(snapshot, ["PLAN_SOURCE_NOT_IN_FROZEN_INPUT"], {"bad": True})
    assert "allowed-version" in prompt
    assert "TELEGRAM" in prompt and "VK" in prompt
    assert "2026-10-01" in prompt and "2026-10-14" in prompt
    assert "total_items" in prompt
    assert "PLAN_SOURCE_NOT_IN_FROZEN_INPUT" in prompt
    assert "ACCESS_TOKEN" not in prompt


def test_planner_repair_is_bounded_to_initial_plus_one() -> None:
    calls = 0
    for _attempt in range(2):
        calls += 1
    assert calls == 2
    assert calls <= 2


def test_near_publication_warning_window_is_two_hours() -> None:
    planned = datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert (
        PublicationPlanService.collision_delta_minutes(planned, planned + timedelta(hours=2)) == 120
    )


@pytest.mark.parametrize("value", ["publication-planner", "placeholder", "unknown"])
def test_planner_rejects_placeholder_model(value: str | None) -> None:
    with pytest.raises(AppError) as exc_info:
        PublicationPlanService.planner_model(value)
    assert exc_info.value.code == "PUBLICATION_PLANNER_MODEL_INVALID"


def test_planner_uses_configured_model_when_agent_model_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "gpt-5.6")
    assert PublicationPlanService.planner_model(None) == "gpt-5.6"


def test_digest_excludes_title_and_balances_beginning_middle_end() -> None:
    version = SimpleNamespace(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        content="\n\n".join(
            ["Заголовок"]
            + [
                f"Ранний substantive claim {index} с достаточным содержанием."
                for index in range(1, 16)
            ]
            + ["Средний раздел"]
            + [
                f"Средний substantive claim {index} с достаточным содержанием."
                for index in range(1, 16)
            ]
            + ["Заключение"]
            + [
                f"Поздний substantive claim {index} с достаточным содержанием."
                for index in range(1, 16)
            ]
        ),
    )
    digest = build_article_planning_digest(version, "Заголовок")
    claims = digest["allowed_claims"]
    assert len(claims) <= 24
    assert all(claim["claim"] != "Заголовок" for claim in claims)
    text = " ".join(claim["claim"] for claim in claims)
    assert "Ранний substantive" in text
    assert "Средний substantive" in text
    assert "Поздний substantive" in text


def test_digest_is_stable_and_traceable() -> None:
    content = (
        "Заголовок\n\nПервая source claim с достаточной длиной.\n\n"
        "Вторая source claim с достаточной длиной."
    )
    version = SimpleNamespace(id=UUID("22222222-2222-2222-2222-222222222222"), content=content)
    first = build_article_planning_digest(version, "Заголовок")
    second = build_article_planning_digest(version, "Заголовок")
    assert first == second
    assert first["allowed_claims"][0]["claim_id"] == "article_22222222_p02"
    assert first["allowed_claims"][0]["source_locator"] == {"paragraph_index": 2}


def test_planner_rejects_missing_configured_model(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openai_default_model", None)
    with pytest.raises(AppError):
        PublicationPlanService.planner_model(None)


@pytest.mark.parametrize(
    ("topic", "angle", "purpose", "brief", "error"),
    [
        (
            "Материал версии f74cd0e0",
            "Ракурс",
            "Результат",
            "Достаточный бриф для проверки",
            "EDITORIAL_INTERNAL_ID",
        ),
        (
            "Стратегическая проблема",
            "ключевой тезис",
            "Результат",
            "Достаточный бриф для проверки",
            "EDITORIAL_ANGLE_TOO_GENERIC",
        ),
        (
            "Стратегическая проблема",
            "Реальный ракурс",
            "Охват",
            "Достаточный бриф для проверки",
            "EDITORIAL_PURPOSE_NOT_MANAGEMENT_OUTCOME",
        ),
        (
            "Стратегическая проблема",
            "Реальный ракурс",
            "Результат",
            "",
            "EDITORIAL_MESSAGE_BRIEF_TOO_SHORT",
        ),
    ],
)
def test_planner_rejects_low_information_editorial_fields(
    topic: str, angle: str, purpose: str, brief: str, error: str
) -> None:
    item = PlanItemInput(
        scheduled_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
        channel=ContentChannel.TELEGRAM,
        source_content_version_id=uuid4(),
        topic=topic,
        angle=angle,
        purpose=purpose,
        format="управленческое наблюдение",
        message_brief=brief or "x",
    )
    assert error in _editorial_validation_errors(item)


def test_planner_accepts_concrete_editorial_fields() -> None:
    item = PlanItemInput(
        scheduled_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
        channel=ContentChannel.TELEGRAM,
        source_content_version_id=uuid4(),
        topic="Почему стратегические решения исчезают в операционной работе",
        angle=(
            "Показать, как отсутствие владельца цели оставляет приоритет "
            "без ежедневного механизма исполнения"
        ),
        purpose=(
            "Помочь руководителю отличить сформулированную стратегию от управляемого исполнения"
        ),
        format="причинно-следственный разбор",
        message_brief=(
            "Команда выходит сессии с решениями, но срочная работа быстро вытесняет их. "
            "Показать связь между владельцем цели и механизмом исполнения, чтобы читатель "
            "увидел, где именно разрывается переход от решения к действию."
        ),
    )
    assert _editorial_validation_errors(item) == []
    planned = datetime(2026, 10, 5, 12, tzinfo=UTC)
    assert (
        PublicationPlanService.collision_delta_minutes(
            planned, planned + timedelta(hours=2, minutes=1)
        )
        is None
    )


async def _planner_fixture(
    session: AsyncSession,
) -> tuple[AgentRun, PublicationPlan, ContentVersion]:
    task, campaign, _ = await smm_fixture(session)
    assert task.assigned_agent_id is not None
    article = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.ARTICLE,
        title="Планировочная статья",
        status=ContentStatus.APPROVED,
        author_agent_id=task.assigned_agent_id,
    )
    session.add(article)
    await session.flush()
    version = ContentVersion(
        content_item_id=article.id,
        version_number=1,
        content="Текст статьи",
        structured_content={"title": article.title, "body": "Текст статьи"},
        created_by_agent_id=task.assigned_agent_id,
    )
    session.add(version)
    await session.flush()
    article.current_version_id = version.id
    session.add(
        Approval(
            object_type=ApprovalObjectType.CONTENT_ITEM,
            object_id=article.id,
            subject_version=1,
            status=ApprovalStatus.APPROVED,
            requested_by_agent_id=task.assigned_agent_id,
            subject_snapshot={"content_version_id": str(version.id)},
            resolved_at=datetime.now(UTC),
        )
    )
    snapshot = {
        "planning_horizon_start": "2026-10-01T00:00:00+00:00",
        "planning_horizon_end": "2026-10-14T00:00:00+00:00",
        "channels": ["TELEGRAM", "VK"],
        "total_items": 1,
        "article_version_ids": [str(version.id)],
        "article_digests": [
            {
                "source_content_version_id": str(version.id),
                "allowed_claims": [
                    {
                        "claim_id": claim_ids_for_version(version.id)[0],
                        "section": "paragraph_1",
                        "claim": "Проверочный источник для планирования.",
                    }
                ],
                "allowed_management_situations": [],
                "allowed_distinctions": [],
                "allowed_consequences": [],
            }
        ],
    }
    task.status = TaskStatus.IN_PROGRESS
    run = AgentRun(
        agent_id=task.assigned_agent_id,
        task_id=task.id,
        campaign_id=campaign.id,
        status=AgentRunStatus.QUEUED,
        input_data={"publication_plan_snapshot": snapshot},
        model="test-model",
        prompt_snapshot="test",
        prompt_hash="test",
    )
    session.add(run)
    await session.flush()
    user = await session.scalar(select(User))
    assert user is not None
    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.DRAFT,
        planning_horizon_start=datetime(2026, 10, 1, tzinfo=UTC),
        planning_horizon_end=datetime(2026, 10, 14, tzinfo=UTC),
        timezone_policy="UTC",
        created_by_user_id=user.id,
        generated_by_agent_run_id=run.id,
    )
    session.add(plan)
    await session.commit()
    return run, plan, version


@pytest.mark.integration
async def test_planner_claim_ids_are_frozen_to_exact_source(db_session: AsyncSession) -> None:
    _run, _plan, version = await _planner_fixture(db_session)
    snapshot = {
        "planning_horizon_start": "2026-10-01T00:00:00+00:00",
        "planning_horizon_end": "2026-10-14T00:00:00+00:00",
        "channels": ["TELEGRAM", "VK"],
        "total_items": 1,
        "article_version_ids": [str(version.id)],
        "article_digests": [
            {
                "source_content_version_id": str(version.id),
                "allowed_claims": [
                    {
                        "claim_id": claim_ids_for_version(version.id)[0],
                        "section": "paragraph_1",
                        "claim": "Проверочный источник для планирования.",
                    }
                ],
            }
        ],
    }
    valid = _planner_result(version.id)
    assert (
        await publication_plan_worker._planner_validation_errors(
            db_session, valid, snapshot, _plan.campaign_id
        )
        == []
    )
    invalid = valid.model_copy(
        update={"items": [valid.items[0].model_copy(update={"source_claim_ids": ["other_claim"]})]}
    )
    errors = await publication_plan_worker._planner_validation_errors(
        db_session, invalid, snapshot, _plan.campaign_id
    )
    assert "PLAN_SOURCE_CLAIM_INVALID" in errors
    missing = valid.model_copy(
        update={"items": [valid.items[0].model_copy(update={"source_claim_ids": []})]}
    )
    missing_errors = await publication_plan_worker._planner_validation_errors(
        db_session, missing, snapshot, _plan.campaign_id
    )
    assert "PLAN_SOURCE_CLAIMS_REQUIRED" in missing_errors


def _planner_result(version_id, *, topic: str = "Тема") -> PublicationPlanAgentResult:
    return PublicationPlanAgentResult(
        rationale="Проверочный план",
        items=[
            PlanItemInput(
                scheduled_at=datetime(2026, 10, 5, 12, tzinfo=UTC),
                channel=ContentChannel.TELEGRAM,
                source_content_version_id=version_id,
                topic=(
                    topic
                    if topic != "Тема"
                    else "Почему выбранные приоритеты исчезают в операционной работе"
                ),
                angle=(
                    "Показать управленческий разрыв между решением команды и владельцем исполнения"
                ),
                purpose=(
                    "Помочь руководителю увидеть, где стратегическое решение "
                    "теряет механизм реализации"
                ),
                format="причинно-следственный разбор",
                message_brief=(
                    "Команда принимает решение, но срочная работа быстро вытесняет его "
                    "из управления. "
                    "Показать, какой владелец и какой механизм обзора возвращают приоритет "
                    "в рабочий контур."
                ),
                source_claim_ids=claim_ids_for_version(version_id),
                source_support_summary="Пункт опирается на проверочное утверждение источника.",
            )
        ],
    )


@pytest.mark.integration
async def test_planner_worker_repair_success_end_to_end(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, plan, version = await _planner_fixture(db_session)
    invalid = _planner_result(uuid4(), topic="Материал версии f74cd0e0")
    valid = _planner_result(version.id)
    outputs = iter([invalid, valid])
    calls: list[str] = []

    class FakeClient:
        async def close(self) -> None:
            return None

    async def fake_run(_agent, prompt, **_kwargs):
        calls.append(prompt)
        return SimpleNamespace(final_output=next(outputs))

    monkeypatch.setattr(publication_plan_worker, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(
        publication_plan_worker,
        "create_worker_session_factory",
        lambda: (engine, async_session_factory),
    )
    monkeypatch.setattr(
        publication_plan_worker, "OpenAIResponsesModel", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(publication_plan_worker.Runner, "run", fake_run)
    monkeypatch.setattr(publication_plan_worker.settings, "openai_api_key", "test")

    await publication_plan_worker._run(run.id)
    assert calls, "worker did not invoke mocked model"
    await db_session.rollback()
    persisted = await db_session.scalar(
        select(PublicationPlan)
        .where(PublicationPlan.id == plan.id)
        .options(selectinload(PublicationPlan.items))
        .execution_options(populate_existing=True)
    )
    debug_run = await db_session.scalar(
        select(AgentRun).where(AgentRun.id == run.id).execution_options(populate_existing=True)
    )
    assert debug_run is not None
    assert debug_run.status is AgentRunStatus.COMPLETED, (
        debug_run.status,
        debug_run.error_code,
        debug_run.error_message,
    )
    assert persisted is not None and persisted.status is PublicationPlanStatus.WAITING_APPROVAL
    assert len(persisted.items) == 1
    assert persisted.items[0].source_content_version_id == version.id
    stored_run = await db_session.get(AgentRun, run.id)
    assert stored_run is not None and stored_run.status is AgentRunStatus.COMPLETED
    assert stored_run.output_data is not None and stored_run.output_data["repair_attempted"] is True
    repair_events = list(
        (
            await db_session.scalars(
                select(ActivityLog).where(
                    ActivityLog.event_type == "PUBLICATION_PLAN_GENERATION_REPAIR_ATTEMPTED"
                )
            )
        ).all()
    )
    assert len(calls) == 2
    assert len(repair_events) == 1
    assert "PLAN_SOURCE_NOT_IN_FROZEN_INPUT" in calls[1]
    assert str(version.id) in calls[1]


@pytest.mark.integration
async def test_planner_worker_repair_exhausted_preserves_existing_items(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, plan, version = await _planner_fixture(db_session)
    invalid = _planner_result(uuid4(), topic="Материал версии f74cd0e0")
    existing = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=1,
        scheduled_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
        channel=ContentChannel.VK,
        source_content_item_id=version.content_item_id,
        source_content_version_id=version.id,
        topic="Существующий",
        angle="Существующий",
        purpose="Существующий",
        format="observation",
        message_brief="Существующий",
        status=PublicationPlanItemStatus.PLANNED,
    )
    db_session.add(existing)
    await db_session.commit()
    before_id = existing.id
    outputs = iter([invalid, invalid])
    calls: list[str] = []

    class FakeClient:
        async def close(self) -> None:
            return None

    async def fake_run(_agent, prompt, **_kwargs):
        calls.append(prompt)
        return SimpleNamespace(final_output=next(outputs))

    monkeypatch.setattr(publication_plan_worker, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(
        publication_plan_worker,
        "create_worker_session_factory",
        lambda: (engine, async_session_factory),
    )
    monkeypatch.setattr(
        publication_plan_worker, "OpenAIResponsesModel", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(publication_plan_worker.Runner, "run", fake_run)
    monkeypatch.setattr(publication_plan_worker.settings, "openai_api_key", "test")

    with pytest.raises(RuntimeError, match="PLAN_REPAIR_EXHAUSTED"):
        await publication_plan_worker._run(run.id)
    await db_session.rollback()
    persisted = await db_session.scalar(
        select(PublicationPlan)
        .where(PublicationPlan.id == plan.id)
        .options(selectinload(PublicationPlan.items))
        .execution_options(populate_existing=True)
    )
    assert persisted is not None and persisted.status is PublicationPlanStatus.DRAFT
    assert len(persisted.items) == 1 and persisted.items[0].id == before_id
    stored_run = await db_session.scalar(
        select(AgentRun).where(AgentRun.id == run.id).execution_options(populate_existing=True)
    )
    assert stored_run is not None and stored_run.status is AgentRunStatus.FAILED
    assert stored_run.output_data is None
    assert len(calls) == 2
    repair_events = list(
        (
            await db_session.scalars(
                select(ActivityLog).where(
                    ActivityLog.event_type == "PUBLICATION_PLAN_GENERATION_REPAIR_ATTEMPTED"
                )
            )
        ).all()
    )
    assert len(repair_events) == 1
