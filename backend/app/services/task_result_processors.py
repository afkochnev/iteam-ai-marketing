from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent_run import AgentRun, ToolCall, ToolCallStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.knowledge import KnowledgeItem, KnowledgeItemStatus
from app.models.knowledge_pack import KnowledgePack, KnowledgePackItem, KnowledgePackStatus
from app.models.content import ContentItem, ContentStatus, ContentType, ContentVersion, ContentVersionSource
from app.models.task import Task, TaskStatus, TaskType
from app.repositories.knowledge_packs import KnowledgePackRepository
from app.schemas.agent_outputs import ArticleWritingResult, CampaignPlan, KnowledgeResearchResult
from app.schemas.knowledge import KnowledgeSearchResult
from app.services.approval_service import ApprovalService
from app.services.knowledge_search_service import build_result_key
from app.services.task_service import TaskService


class TaskResultProcessor(Protocol):
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None: ...


class DefaultTaskResultProcessor:
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None:
        await TaskService(session).complete_task(task.id, output, commit=False)


class CampaignPlanningResultProcessor:
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None:
        try:
            plan = CampaignPlan.model_validate(output)
        except ValidationError as exc:
            raise AppError(
                "INVALID_CAMPAIGN_PLAN", "Структура стратегии не прошла проверку.", 422
            ) from exc
        campaign = (
            await session.execute(
                select(Campaign).where(Campaign.id == task.campaign_id).with_for_update()
            )
        ).scalar_one()
        target_version = int(task.input_data["strategy_version"])
        if (
            campaign.status is not CampaignStatus.PLANNING
            or target_version != campaign.strategy_version + 1
        ):
            raise AppError("INVALID_CAMPAIGN_PLAN", "Версия стратегии больше не актуальна.", 409)
        snapshot = plan.model_dump(mode="json")
        campaign.strategy = snapshot
        campaign.strategy_version = target_version
        campaign.status = CampaignStatus.WAITING_APPROVAL
        await ApprovalService(session).create_strategy_approval(
            campaign.id, target_version, snapshot, run.agent_id
        )
        await TaskService(session).complete_task(task.id, snapshot, commit=False)


class KnowledgeResearchResultProcessor:
    async def process(
        self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]
    ) -> None:
        existing = await KnowledgePackRepository(session).get_by_run(run.id)
        if existing:
            return
        try:
            research = KnowledgeResearchResult.model_validate(output)
        except ValidationError as exc:
            raise AppError(
                "INVALID_KNOWLEDGE_RESEARCH_RESULT",
                "Структура результата исследования не прошла проверку.",
                422,
            ) from exc
        tool_calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.agent_run_id == run.id,
                        ToolCall.tool_name == "search_knowledge",
                        ToolCall.status == ToolCallStatus.COMPLETED,
                    )
                )
            ).all()
        )
        if not tool_calls:
            raise AppError(
                "KNOWLEDGE_TOOL_NOT_USED",
                "Knowledge Keeper не выполнил поиск по базе знаний.",
                422,
            )
        verified: dict[str, tuple[KnowledgeSearchResult, ToolCall]] = {}
        for tool_call in tool_calls:
            result = tool_call.result or {}
            for raw in result.get("results", []):
                try:
                    item = KnowledgeSearchResult.model_validate(raw)
                except ValidationError:
                    continue
                if item.result_key != build_result_key(
                    item.knowledge_item_id, item.file_id, item.excerpt
                ):
                    continue
                verified.setdefault(item.result_key, (item, tool_call))
        selected_keys = [item.result_key for item in research.selected_results]
        if any(key not in verified for key in selected_keys):
            raise AppError(
                "INVALID_KNOWLEDGE_SELECTION",
                "Выбран источник, которого не было в результатах поиска.",
                422,
            )
        selected_rows = [verified[key] for key in selected_keys]
        local_ids = [item.knowledge_item_id for item, _call in selected_rows]
        if local_ids:
            ready_ids = set(
                (
                    await session.scalars(
                        select(KnowledgeItem.id).where(
                            KnowledgeItem.id.in_(local_ids),
                            KnowledgeItem.status == KnowledgeItemStatus.READY,
                        )
                    )
                ).all()
            )
            if any(item_id not in ready_ids for item_id in local_ids):
                raise AppError(
                    "INVALID_KNOWLEDGE_SELECTION",
                    "Один из выбранных источников больше не доступен.",
                    409,
                )
        status = (
            KnowledgePackStatus.READY if research.sufficient else KnowledgePackStatus.INSUFFICIENT
        )
        pack = KnowledgePack(
            campaign_id=task.campaign_id,
            task_id=task.id,
            agent_run_id=run.id,
            created_by_agent_id=run.agent_id,
            strategy_version=_optional_int(task.input_data.get("strategy_version")),
            status=status,
            research_query=research.research_query,
            summary=research.summary,
            gaps=research.gaps,
            metadata_={},
        )
        session.add(pack)
        await session.flush()
        reasons = {item.result_key: item.selection_reason for item in research.selected_results}
        for position, (source, tool_call) in enumerate(selected_rows, start=1):
            session.add(
                KnowledgePackItem(
                    knowledge_pack_id=pack.id,
                    knowledge_item_id=source.knowledge_item_id,
                    tool_call_id=tool_call.id,
                    result_key=source.result_key,
                    source_id=source.source_id,
                    source_title=source.source_title,
                    filename=source.filename,
                    file_id=source.file_id,
                    excerpt=source.excerpt,
                    relevance_score=source.score,
                    selection_reason=reasons[source.result_key],
                    position=position,
                    metadata_=source.metadata,
                )
            )
        output_data: dict[str, object] = {
            "knowledge_pack_id": str(pack.id),
            "status": status.value,
        }
        if research.sufficient:
            await TaskService(session).complete_task(task.id, output_data, commit=False)
        else:
            task.status = TaskStatus.FAILED
            task.output_data = {**output_data, "error_code": "INSUFFICIENT_KNOWLEDGE"}
            task.error_message = "INSUFFICIENT_KNOWLEDGE: Недостаточно материалов в базе знаний."
            task.completed_at = datetime.now(UTC)
            await session.flush()


def render_article_markdown(article: object) -> str:
    draft = ArticleWritingResult.model_validate({"sufficient": True, "article": article}).article
    assert draft is not None
    parts = [f"# {draft.title}"]
    if draft.subtitle:
        parts.append(f"## {draft.subtitle}")
    parts.append(draft.lead)
    for section in draft.sections:
        parts.extend([f"## {section.heading}", section.body_markdown])
    parts.extend(["## Заключение", draft.conclusion, draft.cta])
    return "\n\n".join(parts)


class WriterResultProcessor:
    async def process(self, session: AsyncSession, run: AgentRun, task: Task, output: dict[str, object]) -> None:
        existing = await session.scalar(select(ContentVersion).where(ContentVersion.source_agent_run_id == run.id))
        if existing:
            return
        try:
            result = ArticleWritingResult.model_validate(output)
        except ValidationError as exc:
            raise AppError("INVALID_ARTICLE_RESULT", "Структура статьи не прошла проверку.", 422) from exc
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.agent_run_id == run.id, ToolCall.tool_name == "read_knowledge_pack", ToolCall.status == ToolCallStatus.COMPLETED))).all())
        if not calls:
            raise AppError("KNOWLEDGE_PACK_NOT_READ", "Writer не прочитал пакет знаний.", 422)
        allowed = {UUID(value) for value in task.input_data.get("allowed_knowledge_pack_ids", [])}
        read_pack_ids: set[UUID] = set()
        verified: set[UUID] = set()
        for call in calls:
            payload = call.result or {}
            if payload.get("pack_id"):
                read_pack_ids.add(UUID(str(payload["pack_id"])))
            for item in payload.get("items", []):
                if item.get("knowledge_pack_item_id"):
                    verified.add(UUID(str(item["knowledge_pack_item_id"])))
        if not allowed.issubset(read_pack_ids):
            raise AppError("KNOWLEDGE_PACK_NOT_READ", "Writer не прочитал все необходимые пакеты знаний.", 422)
        if not result.sufficient:
            task.status = TaskStatus.FAILED
            task.error_message = "INSUFFICIENT_ARTICLE_EVIDENCE: Недостаточно подтверждённых материалов для статьи."
            task.output_data = {"gaps": result.gaps, "error_code": "INSUFFICIENT_ARTICLE_EVIDENCE"}
            task.completed_at = datetime.now(UTC)
            await session.flush()
            return
        assert result.article is not None
        selected = [source_id for section in result.article.sections for source_id in section.knowledge_pack_item_ids]
        if not selected:
            raise AppError("INVALID_ARTICLE_SOURCE", "Статья не содержит подтверждённых источников.", 422)
        if any(source_id not in verified for source_id in selected):
            raise AppError("INVALID_ARTICLE_SOURCE", "Статья содержит неподтверждённый источник.", 422)
        rows = {item.id: item for item in (await session.scalars(select(KnowledgePackItem).where(KnowledgePackItem.id.in_(selected)))).all()}
        if any(source_id not in rows for source_id in selected):
            raise AppError("INVALID_ARTICLE_SOURCE", "Источник статьи не найден.", 422)
        item = ContentItem(campaign_id=task.campaign_id, source_task_id=task.id, content_type=ContentType.ARTICLE, title=result.article.title, status=ContentStatus.DRAFT, author_agent_id=run.agent_id, metadata_={})
        session.add(item)
        await session.flush()
        version = ContentVersion(content_item_id=item.id, version_number=1, content=render_article_markdown(result.article), structured_content=result.article.model_dump(mode="json"), created_by_agent_id=run.agent_id, source_agent_run_id=run.id, change_description="Initial article draft generated by Writer")
        session.add(version)
        await session.flush()
        item.current_version_id = version.id
        for position, section in enumerate(result.article.sections, start=1):
            for source_position, source_id in enumerate(section.knowledge_pack_item_ids, start=1):
                session.add(ContentVersionSource(content_version_id=version.id, knowledge_pack_item_id=source_id, section_key=section.key, position=(position * 1000) + source_position))
        await TaskService(session).complete_task(task.id, {"content_item_id": str(item.id), "content_version_id": str(version.id), "content_type": "ARTICLE"}, commit=False)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


class TaskResultProcessorRegistry:
    def get(self, task_type: TaskType) -> TaskResultProcessor:
        if task_type is TaskType.CAMPAIGN_PLANNING:
            return CampaignPlanningResultProcessor()
        if task_type is TaskType.KNOWLEDGE_RESEARCH:
            return KnowledgeResearchResultProcessor()
        if task_type is TaskType.WRITE_ARTICLE:
            return WriterResultProcessor()
        return DefaultTaskResultProcessor()


result_processor_registry = TaskResultProcessorRegistry()
