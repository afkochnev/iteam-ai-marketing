from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from app.core.config import settings
from app.core.runtime_admission import require_active_runtime


@dataclass(frozen=True)
class ProviderSearchResult:
    file_id: str
    filename: str
    score: float | None
    excerpt: str
    attributes: dict[str, Any]


class OpenAIKnowledgeProvider:
    def __init__(self) -> None:
        require_active_runtime()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_NOT_CONFIGURED")
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def retrieve_store(self, store_id: str) -> tuple[str, str]:
        require_active_runtime()
        store = await self.client.vector_stores.retrieve(store_id)
        return store.id, store.name or "iTeam Knowledge Base"

    async def create_store(self, name: str) -> tuple[str, str]:
        require_active_runtime()
        store = await self.client.vector_stores.create(name=name)
        return store.id, store.name or name

    async def upload_file(self, filename: str, content: bytes) -> str:
        require_active_runtime()
        uploaded = await self.client.files.create(file=(filename, content), purpose="assistants")
        return uploaded.id

    async def attach_file(
        self, store_id: str, file_id: str, attributes: dict[str, str | float | bool]
    ) -> tuple[str, str]:
        require_active_runtime()
        item = await self.client.vector_stores.files.create(
            store_id, file_id=file_id, attributes=attributes
        )
        return item.id, item.status

    async def get_file_status(self, store_id: str, file_id: str) -> str:
        require_active_runtime()
        item = await self.client.vector_stores.files.retrieve(file_id, vector_store_id=store_id)
        return item.status

    async def detach_file(self, store_id: str, file_id: str) -> None:
        require_active_runtime()
        await self.client.vector_stores.files.delete(file_id, vector_store_id=store_id)

    async def search(
        self, store_id: str, query: str, max_results: int
    ) -> list[ProviderSearchResult]:
        require_active_runtime()
        page = await self.client.vector_stores.search(
            store_id, query=query, max_num_results=max_results
        )
        results: list[ProviderSearchResult] = []
        async for item in page:
            excerpts = [part.text for part in item.content if part.type == "text"]
            results.append(
                ProviderSearchResult(
                    file_id=item.file_id,
                    filename=item.filename,
                    score=item.score,
                    excerpt="\n".join(excerpts),
                    attributes=dict(item.attributes or {}),
                )
            )
        return results
