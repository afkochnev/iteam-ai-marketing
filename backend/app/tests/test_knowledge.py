from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents.tool_context import ToolContext
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.factory import AgentRuntimeContext
from app.agents.knowledge_tools import search_knowledge
from app.agents.tool_registry import tool_registry
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.integrations.openai_knowledge import ProviderSearchResult
from app.models.agent_run import ToolCall, ToolCallStatus
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
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.services.agent_run_service import AgentRunService
from app.services.knowledge_search_service import KnowledgeSearchService
from app.services.knowledge_service import KnowledgeService
from app.services.knowledge_store_service import KnowledgeStoreService
from app.tests.test_agent_runtime import runtime_fixture
from app.workers.knowledge_worker import _index


class FakeProvider:
    def __init__(self) -> None:
        self.created = 0
        self.uploaded = 0
        self.detached: list[str] = []
        self.search_results: list[ProviderSearchResult] = []

    async def retrieve_store(self, store_id: str) -> tuple[str, str]:
        return store_id, "Imported store"

    async def create_store(self, name: str) -> tuple[str, str]:
        self.created += 1
        return "vs_test", name

    async def upload_file(self, filename: str, content: bytes) -> str:
        self.uploaded += 1
        return f"file_{filename}"

    async def attach_file(
        self, store_id: str, file_id: str, attributes: dict[str, str | float | bool]
    ) -> tuple[str, str]:
        return file_id, "completed"

    async def get_file_status(self, store_id: str, file_id: str) -> str:
        return "completed"

    async def detach_file(self, store_id: str, file_id: str) -> None:
        self.detached.append(file_id)

    async def search(
        self, store_id: str, query: str, max_results: int
    ) -> list[ProviderSearchResult]:
        return self.search_results[:max_results]


async def user(session: AsyncSession, role: UserRole = UserRole.ADMIN) -> User:
    result = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Knowledge User",
        role=role,
    )
    await session.commit()
    return result


async def source(session: AsyncSession) -> KnowledgeSource:
    result = KnowledgeSource(
        name="Ручные загрузки",
        source_type=KnowledgeSourceType.FILE_UPLOAD,
        status=KnowledgeSourceStatus.ACTIVE,
        metadata_={},
    )
    session.add(result)
    await session.commit()
    return result


async def login(client: AsyncClient, account: User) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": account.email, "password": "password"}
    )
    assert response.status_code == 200


async def test_store_initialize_is_idempotent_and_bootstraps(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    service = KnowledgeStoreService(db_session, provider)  # type: ignore[arg-type]
    first = await service.initialize()
    second = await service.initialize()
    assert first.id == second.id
    assert first.provider is KnowledgeStoreProvider.OPENAI
    assert provider.created == 1

    first.is_active = False
    await db_session.commit()
    monkeypatch.setattr(settings, "openai_vector_store_id", "vs_existing")
    imported = await service.initialize()
    assert imported.external_store_id == "vs_existing"


async def test_upload_validation_and_lifecycle(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    await source(db_session)
    provider = FakeProvider()
    service = KnowledgeService(db_session, provider)  # type: ignore[arg-type]
    item = await service.upload(
        filename="../management.md",
        content=b"regular management",
        mime_type="text/markdown",
        title=None,
        author=" iTeam ",
        created_by=owner.id,
    )
    assert item.status is KnowledgeItemStatus.INDEXING
    assert item.original_filename == "management.md"
    assert item.created_by == owner.id
    assert item.openai_file_id == "file_management.md"
    assert provider.uploaded == 1
    with pytest.raises(AppError):
        await service.upload(
            filename="bad.exe",
            content=b"x",
            mime_type=None,
            title=None,
            author=None,
            created_by=owner.id,
        )
    with pytest.raises(AppError):
        await service.upload(
            filename="empty.txt",
            content=b"",
            mime_type=None,
            title=None,
            author=None,
            created_by=owner.id,
        )


async def test_search_maps_provenance_and_skips_unknown(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    item_source = await source(db_session)
    store = KnowledgeStore(
        provider=KnowledgeStoreProvider.OPENAI,
        name="KB",
        external_store_id="vs_test",
        status=KnowledgeStoreStatus.ACTIVE,
        is_active=True,
        metadata_={},
    )
    item = KnowledgeItem(
        source_id=item_source.id,
        title="Management",
        content_type="md",
        original_filename="management.md",
        openai_file_id="file_known",
        status=KnowledgeItemStatus.READY,
        metadata_={},
        created_by=owner.id,
    )
    db_session.add_all([store, item])
    await db_session.commit()
    provider = FakeProvider()
    provider.search_results = [
        ProviderSearchResult("file_known", "management.md", 0.91, "управленческий ритм", {}),
        ProviderSearchResult("file_foreign", "foreign.md", 0.99, "foreign", {}),
    ]
    response = await KnowledgeSearchService(db_session, provider).search("ритм", 10)  # type: ignore[arg-type]
    assert response.result_count == 1
    assert response.results[0].knowledge_item_id == item.id
    assert response.results[0].source_title == "Ручные загрузки"
    assert response.results[0].file_id == "file_known"
    assert response.results[0].score == 0.91


async def test_archive_detaches_and_excludes_from_search(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    item_source = await source(db_session)
    store = KnowledgeStore(
        provider=KnowledgeStoreProvider.OPENAI,
        name="KB",
        external_store_id="vs_test",
        status=KnowledgeStoreStatus.ACTIVE,
        is_active=True,
        metadata_={},
    )
    item = KnowledgeItem(
        source_id=item_source.id,
        title="Doc",
        content_type="txt",
        openai_file_id="file_1",
        vector_store_file_id="file_1",
        status=KnowledgeItemStatus.READY,
        metadata_={},
        created_by=owner.id,
    )
    db_session.add_all([store, item])
    await db_session.commit()
    provider = FakeProvider()
    archived = await KnowledgeService(db_session, provider).archive(item.id)  # type: ignore[arg-type]
    assert archived.status is KnowledgeItemStatus.ARCHIVED
    assert archived.archived_at is not None
    assert provider.detached == ["file_1"]
    provider.search_results = [ProviderSearchResult("file_1", "doc.txt", 1.0, "text", {})]
    assert (await KnowledgeSearchService(db_session, provider).search("text")).result_count == 0  # type: ignore[arg-type]


async def test_knowledge_api_rbac(client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await user(db_session, UserRole.MANAGER)
    await source(db_session)
    await login(client, manager)
    assert (await client.get("/api/v1/knowledge/items")).status_code == 200
    upload = await client.post(
        "/api/v1/knowledge/upload", files={"file": ("doc.txt", b"text", "text/plain")}
    )
    assert upload.status_code == 403
    assert (await client.post("/api/v1/knowledge/store/initialize")).status_code == 403


def test_search_tool_registered() -> None:
    assert tool_registry.missing(["search_knowledge"]) == []
    assert len(tool_registry.resolve(["search_knowledge"])) == 1
    assert tool_registry.resolve([]) == []


async def test_search_tool_audits_success_and_failure(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openai_default_model", "test-model")
    _, _, _, task = await runtime_fixture(db_session)
    run = await AgentRunService(db_session).create_queued_run(task.id)
    context = ToolContext(
        AgentRuntimeContext(run.agent_id, run.task_id, run.campaign_id, run.id),
        usage=SimpleNamespace(),
        tool_name="search_knowledge",
        tool_call_id="call_1",
        tool_arguments='{"query":"ритм","max_results":10}',
    )

    async def successful_search(
        _self: KnowledgeSearchService, query: str, max_results: int = 10
    ) -> object:
        from app.schemas.knowledge import KnowledgeSearchResponse

        return KnowledgeSearchResponse(query=query, result_count=0, results=[])

    monkeypatch.setattr(KnowledgeSearchService, "search", successful_search)
    result = await search_knowledge.on_invoke_tool(context, '{"query":"ритм","max_results":10}')
    assert '"result_count": 0' in result
    audits = list((await db_session.scalars(select(ToolCall).order_by(ToolCall.created_at))).all())
    assert audits[-1].status is ToolCallStatus.COMPLETED
    assert audits[-1].agent_run_id == run.id
    assert audits[-1].arguments["query"] == "ритм"

    async def failed_search(
        _self: KnowledgeSearchService, query: str, max_results: int = 10
    ) -> object:
        raise AppError("KNOWLEDGE_SEARCH_FAILED", "safe", 502)

    monkeypatch.setattr(KnowledgeSearchService, "search", failed_search)
    await search_knowledge.on_invoke_tool(context, '{"query":"ошибка","max_results":10}')
    db_session.expire_all()
    audits = list((await db_session.scalars(select(ToolCall).order_by(ToolCall.created_at))).all())
    assert audits[-1].status is ToolCallStatus.FAILED
    assert audits[-1].error_message == "Поиск по базе знаний завершился ошибкой."


async def test_index_worker_ready_and_duplicate_delivery(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await user(db_session)
    item_source = await source(db_session)
    db_session.add(
        KnowledgeStore(
            provider=KnowledgeStoreProvider.OPENAI,
            name="KB",
            external_store_id="vs_test",
            status=KnowledgeStoreStatus.ACTIVE,
            is_active=True,
            metadata_={},
        )
    )
    item = KnowledgeItem(
        source_id=item_source.id,
        title="Index me",
        content_type="md",
        openai_file_id="file_index",
        status=KnowledgeItemStatus.INDEXING,
        metadata_={},
        created_by=owner.id,
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    provider = FakeProvider()
    monkeypatch.setattr(
        "app.integrations.openai_knowledge.OpenAIKnowledgeProvider", lambda: provider
    )
    await _index(item_id)
    db_session.expire_all()
    stored = await db_session.get(KnowledgeItem, item_id)
    assert stored is not None and stored.status is KnowledgeItemStatus.READY
    assert stored.vector_store_file_id == "file_index"
    assert stored.indexed_at is not None
    await _index(item_id)
    assert stored.status is KnowledgeItemStatus.READY
