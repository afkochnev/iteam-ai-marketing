from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.output_registry import output_type_registry
from app.core.errors import AppError
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus, AgentTool
from app.models.agent_run import AgentRunStatus, ToolCall, ToolCallStatus
from app.models.campaign import CampaignStatus
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
from app.models.knowledge_pack import KnowledgePack, KnowledgePackStatus
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import UserRole
from app.repositories.knowledge_packs import KnowledgePackRepository
from app.repositories.users import UserRepository
from app.schemas.agent_outputs import KnowledgeResearchResult
from app.schemas.campaign import CampaignCreate
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import RuntimeResult
from app.services.campaign_service import CampaignService
from app.services.knowledge_search_service import build_result_key
from app.services.task_result_processors import KnowledgeResearchResultProcessor
from app.services.task_service import TaskService


def research_output(
    *,
    key: str | None = None,
    sufficient: bool = True,
    gaps: list[str] | None = None,
    research_query: str = "управленческий ритм",
    summary: str = "Найдены проверенные материалы.",
) -> dict[str, object]:
    return {
        "research_query": research_query,
        "summary": summary,
        "sufficient": sufficient,
        "selected_results": (
            [{"result_key": key, "selection_reason": "Прямо раскрывает тему."}] if key else []
        ),
        "gaps": gaps
        if gaps is not None
        else ([] if sufficient else ["Нет данных о компаниях нужного масштаба."]),
    }


def test_knowledge_research_schema_rules() -> None:
    key = "a" * 64
    assert KnowledgeResearchResult.model_validate(research_output(key=key)).sufficient
    assert output_type_registry.get(TaskType.KNOWLEDGE_RESEARCH) is KnowledgeResearchResult
    invalid = [
        {**research_output(key=key), "research_query": "   "},
        {**research_output(key=key), "summary": ""},
        {**research_output(key=None), "sufficient": True},
        {**research_output(key=None, sufficient=False), "gaps": []},
        {
            **research_output(key=key),
            "selected_results": [
                {"result_key": key, "selection_reason": "one"},
                {"result_key": key, "selection_reason": "two"},
            ],
        },
        {
            **research_output(key=key),
            "selected_results": [
                {"result_key": str(index).zfill(64), "selection_reason": "reason"}
                for index in range(13)
            ],
        },
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            KnowledgeResearchResult.model_validate(payload)


async def knowledge_fixture(
    session: AsyncSession,
    *,
    with_store: bool = True,
    agent_slug: str = "knowledge_keeper",
    tool_enabled: bool = True,
) -> tuple[Agent, Task, Task, KnowledgeItem, str]:
    owner = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Owner",
        role=UserRole.ADMIN,
    )
    agent = Agent(
        name="Knowledge Keeper",
        slug=agent_slug,
        role="knowledge_keeper",
        system_prompt="Use search_knowledge.",
        model="test-model",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    session.add(agent)
    await session.flush()
    session.add(
        AgentTool(
            agent_id=agent.id,
            tool_name="search_knowledge",
            is_enabled=tool_enabled,
        )
    )
    campaign = await CampaignService(session).create_campaign(
        CampaignCreate(name="Knowledge Campaign", goal="Create expert content"), owner
    )
    campaign.status = CampaignStatus.ACTIVE
    source = KnowledgeSource(
        name="Ручные загрузки",
        source_type=KnowledgeSourceType.FILE_UPLOAD,
        status=KnowledgeSourceStatus.ACTIVE,
        metadata_={},
    )
    session.add(source)
    await session.flush()
    item = KnowledgeItem(
        source_id=source.id,
        title="Regular management",
        content_type="md",
        original_filename="management.md",
        openai_file_id="file_management",
        status=KnowledgeItemStatus.READY,
        metadata_={},
        created_by=owner.id,
    )
    session.add(item)
    if with_store:
        session.add(
            KnowledgeStore(
                provider=KnowledgeStoreProvider.OPENAI,
                name="KB",
                external_store_id="vs_test",
                status=KnowledgeStoreStatus.ACTIVE,
                is_active=True,
                metadata_={},
            )
        )
    await session.commit()
    research = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.KNOWLEDGE_RESEARCH,
            title="Research",
            assigned_agent_id=agent.id,
            input_data={"brief": "Find facts", "strategy_version": 1},
        )
    )
    article = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.WRITE_ARTICLE,
            title="Article",
            dependency_ids=[research.id],
        )
    )
    result_key = build_result_key(
        item.id, "file_management", "Проверенный фрагмент об управленческом ритме."
    )
    return agent, research, article, item, result_key


async def add_search_call(
    session: AsyncSession,
    run_id: object,
    item: KnowledgeItem,
    result_key: str,
    *,
    query: str = "ритм",
    excerpt: str = "Проверенный фрагмент об управленческом ритме.",
) -> ToolCall:
    call = ToolCall(
        agent_run_id=run_id,
        tool_name="search_knowledge",
        arguments={"query": query, "max_results": 10},
        result={
            "query": query,
            "result_count": 1,
            "results": [
                {
                    "result_key": result_key,
                    "knowledge_item_id": str(item.id),
                    "source_id": str(item.source_id),
                    "source_title": "Ручные загрузки",
                    "filename": "management.md",
                    "file_id": "file_management",
                    "excerpt": excerpt,
                    "score": 0.91,
                    "metadata": {},
                }
            ],
        },
        status=ToolCallStatus.COMPLETED,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    session.add(call)
    await session.flush()
    return call


async def test_runtime_policy(db_session: AsyncSession) -> None:
    _agent, task, _article, _item, _key = await knowledge_fixture(db_session)
    run = await AgentRunService(db_session).create_queued_run(task.id)
    assert run.status is AgentRunStatus.QUEUED


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"with_store": False}, "KNOWLEDGE_STORE_NOT_CONFIGURED"),
        ({"agent_slug": "writer"}, "INVALID_AGENT_FOR_TASK_TYPE"),
        ({"tool_enabled": False}, "REQUIRED_AGENT_TOOL_UNAVAILABLE"),
    ],
)
async def test_runtime_policy_rejections(
    db_session: AsyncSession, kwargs: dict[str, object], code: str
) -> None:
    _agent, task, _article, _item, _key = await knowledge_fixture(db_session, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).create_queued_run(task.id)
    assert error.value.code == code


async def test_success_builds_verified_pack_and_unblocks_article(
    db_session: AsyncSession,
) -> None:
    _agent, task, article, item, key = await knowledge_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    tool_call = await add_search_call(db_session, run.id, item, key)
    await db_session.commit()
    await service.finish_success(
        run.id, RuntimeResult(research_output(key=key), 2, 20, 10, 30, None)
    )
    pack = await KnowledgePackRepository(db_session).get_by_run(run.id)
    assert pack is not None and pack.status is KnowledgePackStatus.READY
    assert len(pack.items) == 1
    assert pack.items[0].tool_call_id == tool_call.id
    assert pack.items[0].excerpt == "Проверенный фрагмент об управленческом ритме."
    assert pack.items[0].selection_reason == "Прямо раскрывает тему."
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.COMPLETED
    assert (await TaskService(db_session).get_task(article.id)).status is TaskStatus.READY


async def test_sufficient_with_gaps_is_deliverable_aware_and_unblocks_writer(
    db_session: AsyncSession,
) -> None:
    _agent, task, article, item, _key = await knowledge_fixture(db_session)
    task.input_data = {
        **task.input_data,
        "brief": "Стратегическая или форсайт-сессия: какой формат нужен компании",
    }
    excerpt = (
        "Методология стратегической и форсайт-сессии, подготовка, формат, результаты "
        "и внедрение через проекты и governance; есть качественные отзывы клиентов."
    )
    key = build_result_key(item.id, "file_management", excerpt)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_search_call(
        db_session,
        run.id,
        item,
        key,
        query="стратегическая форсайт сессия формат",
        excerpt=excerpt,
    )
    await db_session.commit()
    gaps = ["Нет количественного before/after кейса.", "Нет формальной матрицы выбора."]
    await service.finish_success(
        run.id,
        RuntimeResult(
            research_output(
                key=key,
                gaps=gaps,
                research_query="стратегическая форсайт сессия формат",
                summary="Методология, подготовка и результаты форматов подтверждены источником.",
            ),
            1,
            10,
            5,
            15,
            None,
        ),
    )
    pack = await KnowledgePackRepository(db_session).get_by_run(run.id)
    assert pack is not None and pack.status is KnowledgePackStatus.READY
    assert pack.gaps == gaps
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.COMPLETED
    assert (await TaskService(db_session).get_task(article.id)).status is TaskStatus.READY


async def test_tangential_sources_remain_insufficient(
    db_session: AsyncSession,
) -> None:
    _agent, task, article, item, key = await knowledge_fixture(db_session)
    task.input_data = {
        **task.input_data,
        "brief": "Стратегическая форсайт-сессия для компании",
    }
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_search_call(db_session, run.id, item, key)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(research_output(key=key), 1, 10, 5, 15, None),
    )
    pack = await KnowledgePackRepository(db_session).get_by_run(run.id)
    assert pack is not None and pack.status is KnowledgePackStatus.INSUFFICIENT
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.FAILED
    assert stored_task.output_data["error_code"] == "INSUFFICIENT_KNOWLEDGE"
    assert (await TaskService(db_session).get_task(article.id)).status is TaskStatus.BLOCKED


async def test_quantitative_brief_requires_quantitative_grounding(
    db_session: AsyncSession,
) -> None:
    _agent, task, article, item, _key = await knowledge_fixture(db_session)
    task.input_data = {
        **task.input_data,
        "brief": "Prove quantitative ROI with before/after client metrics",
    }
    excerpt = "Методология стратегической сессии и качественные отзывы клиентов."
    key = build_result_key(item.id, "file_management", excerpt)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_search_call(
        db_session,
        run.id,
        item,
        key,
        query="strategic session methodology",
        excerpt=excerpt,
    )
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(
            research_output(
                key=key,
                research_query="strategic session methodology",
                summary="Методология подтверждена, но quantitative ROI не найден.",
                gaps=["Нет quantitative ROI и before/after metrics."],
            ),
            1,
            10,
            5,
            15,
            None,
        ),
    )
    pack = await KnowledgePackRepository(db_session).get_by_run(run.id)
    assert pack is not None and pack.status is KnowledgePackStatus.INSUFFICIENT
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.FAILED
    assert (await TaskService(db_session).get_task(article.id)).status is TaskStatus.BLOCKED


async def test_insufficient_pack_fails_business_task_and_keeps_downstream_blocked(
    db_session: AsyncSession,
) -> None:
    _agent, task, article, item, key = await knowledge_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_search_call(db_session, run.id, item, key)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(research_output(key=None, sufficient=False), 1, 10, 5, 15, None),
    )
    pack = await KnowledgePackRepository(db_session).get_by_run(run.id)
    assert pack is not None and pack.status is KnowledgePackStatus.INSUFFICIENT
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.FAILED
    assert stored_task.output_data["error_code"] == "INSUFFICIENT_KNOWLEDGE"
    assert (await TaskService(db_session).get_task(article.id)).status is TaskStatus.BLOCKED


async def test_missing_tool_hallucination_and_archived_race_are_rejected(
    db_session: AsyncSession,
) -> None:
    _agent, task, _article, item, key = await knowledge_fixture(db_session)
    item_id = item.id
    service = AgentRunService(db_session)
    no_tool_run = await service.create_queued_run(task.id)
    assert await service.claim(no_tool_run.id)
    await service.finish_success(
        no_tool_run.id, RuntimeResult(research_output(key=key), 1, 1, 1, 2, None)
    )
    assert (await service.get_run(no_tool_run.id)).error_code == "KNOWLEDGE_TOOL_NOT_USED"

    task.status = TaskStatus.READY
    task.error_message = None
    await db_session.commit()
    bad_run = await service.create_queued_run(task.id)
    assert await service.claim(bad_run.id)
    current_item = await db_session.get(KnowledgeItem, item_id)
    assert current_item is not None
    await add_search_call(db_session, bad_run.id, current_item, key)
    await db_session.commit()
    await service.finish_success(
        bad_run.id,
        RuntimeResult(research_output(key="b" * 64), 1, 1, 1, 2, None),
    )
    assert (await service.get_run(bad_run.id)).error_code == "INVALID_KNOWLEDGE_SELECTION"

    task.status = TaskStatus.READY
    task.error_message = None
    await db_session.commit()
    archived_run = await service.create_queued_run(task.id)
    assert await service.claim(archived_run.id)
    current_item = await db_session.get(KnowledgeItem, item_id)
    assert current_item is not None
    await add_search_call(db_session, archived_run.id, current_item, key)
    current_item.status = KnowledgeItemStatus.ARCHIVED
    await db_session.commit()
    await service.finish_success(
        archived_run.id, RuntimeResult(research_output(key=key), 1, 1, 1, 2, None)
    )
    assert (await service.get_run(archived_run.id)).error_code == "INVALID_KNOWLEDGE_SELECTION"
    assert not (
        await db_session.scalars(
            select(KnowledgePack).where(KnowledgePack.agent_run_id == archived_run.id)
        )
    ).first()


async def test_processor_idempotency(db_session: AsyncSession) -> None:
    _agent, task, _article, item, key = await knowledge_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_search_call(db_session, run.id, item, key)
    await db_session.commit()
    processor = KnowledgeResearchResultProcessor()
    await processor.process(db_session, run, task, research_output(key=key))
    await processor.process(db_session, run, task, research_output(key=key))
    await db_session.commit()
    packs = list(
        (
            await db_session.scalars(
                select(KnowledgePack).where(KnowledgePack.agent_run_id == run.id)
            )
        ).all()
    )
    assert len(packs) == 1


async def test_retry_after_insufficient_preserves_history_and_succeeds(
    db_session: AsyncSession,
) -> None:
    _agent, task, article, item, key = await knowledge_fixture(db_session)
    service = AgentRunService(db_session)
    first = await service.create_queued_run(task.id)
    assert await service.claim(first.id)
    await add_search_call(db_session, first.id, item, key)
    await db_session.commit()
    await service.finish_success(
        first.id,
        RuntimeResult(research_output(key=None, sufficient=False), 1, 5, 5, 10, None),
    )
    second = await service.create_queued_run(task.id, retry=True)
    assert second.id != first.id
    assert await service.claim(second.id)
    current_item = await db_session.get(KnowledgeItem, item.id)
    assert current_item is not None
    await add_search_call(db_session, second.id, current_item, key)
    await db_session.commit()
    await service.finish_success(
        second.id, RuntimeResult(research_output(key=key), 1, 5, 5, 10, None)
    )
    packs = await KnowledgePackRepository(db_session).list_packs(task_id=task.id)
    assert {pack.status for pack in packs} == {
        KnowledgePackStatus.INSUFFICIENT,
        KnowledgePackStatus.READY,
    }
    assert (await TaskService(db_session).get_task(task.id)).status is TaskStatus.COMPLETED
    assert (await TaskService(db_session).get_task(article.id)).status is TaskStatus.READY


async def test_inactive_keeper_is_rejected(db_session: AsyncSession) -> None:
    agent, task, _article, _item, _key = await knowledge_fixture(db_session)
    agent.status = AgentStatus.INACTIVE
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await AgentRunService(db_session).create_queued_run(task.id)
    assert error.value.code == "AGENT_INACTIVE"


async def test_knowledge_pack_api(client: AsyncClient, db_session: AsyncSession) -> None:
    _agent, task, _article, item, key = await knowledge_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_search_call(db_session, run.id, item, key)
    await db_session.commit()
    await service.finish_success(run.id, RuntimeResult(research_output(key=key), 1, 5, 5, 10, None))
    owner = await UserRepository(db_session).create(
        email=f"api-{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="API User",
        role=UserRole.MANAGER,
    )
    await db_session.commit()
    login = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": "password"}
    )
    assert login.status_code == 200
    listed = await client.get(f"/api/v1/knowledge-packs?task_id={task.id}")
    assert listed.status_code == 200 and len(listed.json()) == 1
    detail = await client.get(f"/api/v1/knowledge-packs/{listed.json()[0]['id']}")
    assert detail.status_code == 200
    assert detail.json()["items"][0]["excerpt"].startswith("Проверенный")
