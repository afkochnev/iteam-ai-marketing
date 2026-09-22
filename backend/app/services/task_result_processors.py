from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent_run import AgentRun, ToolCall, ToolCallStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.knowledge import KnowledgeItem, KnowledgeItemStatus
from app.models.knowledge_pack import KnowledgePack, KnowledgePackItem, KnowledgePackStatus
from app.models.task import Task, TaskStatus, TaskType
from app.repositories.knowledge_packs import KnowledgePackRepository
from app.schemas.agent_outputs import CampaignPlan, KnowledgeResearchResult
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


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


class TaskResultProcessorRegistry:
    def get(self, task_type: TaskType) -> TaskResultProcessor:
        if task_type is TaskType.CAMPAIGN_PLANNING:
            return CampaignPlanningResultProcessor()
        if task_type is TaskType.KNOWLEDGE_RESEARCH:
            return KnowledgeResearchResultProcessor()
        return DefaultTaskResultProcessor()


result_processor_registry = TaskResultProcessorRegistry()
