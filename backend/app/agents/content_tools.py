import json
import logging
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from agents import RunContextWrapper, function_tool
from sqlalchemy import select

from app.agents.factory import AgentRuntimeContext
from app.agents.tool_registry import tool_registry
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.agent_run import ToolCall, ToolCallStatus
from app.models.knowledge_pack import KnowledgePack, KnowledgePackStatus

logger = logging.getLogger(__name__)


@function_tool
async def read_knowledge_pack(
    ctx: RunContextWrapper[AgentRuntimeContext], knowledge_pack_id: UUID
) -> str:
    runtime = ctx.context
    if knowledge_pack_id not in runtime.allowed_knowledge_pack_ids:
        raise AppError(
            "KNOWLEDGE_PACK_ACCESS_DENIED",
            "Пакет знаний недоступен этому запуску.",
            403,
        )
    arguments = {"knowledge_pack_id": str(knowledge_pack_id)}
    factory = runtime.session_factory or async_session_factory
    started = monotonic()
    async with factory() as session:
        call = ToolCall(
            agent_run_id=runtime.agent_run_id,
            tool_name="read_knowledge_pack",
            arguments=arguments,
            status=ToolCallStatus.STARTED,
            started_at=datetime.now(UTC),
        )
        session.add(call)
        await session.commit()
        try:
            pack = (
                await session.execute(
                    select(KnowledgePack).where(
                        KnowledgePack.id == knowledge_pack_id,
                        KnowledgePack.status == KnowledgePackStatus.READY,
                    )
                )
            ).scalar_one_or_none()
            if pack is None:
                raise AppError("KNOWLEDGE_PACK_NOT_AVAILABLE", "Пакет знаний недоступен.", 409)
            result = {
                "pack_id": str(pack.id),
                "research_query": pack.research_query,
                "summary": pack.summary,
                "gaps": list(pack.gaps or []),
                "items": [
                    {
                        "knowledge_pack_item_id": str(item.id),
                        "source_title": item.source_title,
                        "filename": item.filename,
                        "excerpt": item.excerpt,
                        "relevance_score": item.relevance_score,
                        "selection_reason": item.selection_reason,
                    }
                    for item in pack.items
                ],
            }
            call.result = result
            call.status = ToolCallStatus.COMPLETED
            call.completed_at = datetime.now(UTC)
            await session.commit()
            logger.info(
                "Agent tool completed",
                extra={
                    "event": "agent_tool_completed",
                    "tool_name": "read_knowledge_pack",
                    "agent_run_id": str(runtime.agent_run_id),
                    "task_id": str(runtime.task_id),
                    "elapsed_ms": round((monotonic() - started) * 1000, 2),
                },
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.exception(
                "Agent tool failed",
                extra={
                    "event": "agent_tool_failed",
                    "tool_name": "read_knowledge_pack",
                    "agent_run_id": str(runtime.agent_run_id),
                    "task_id": str(runtime.task_id),
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": round((monotonic() - started) * 1000, 2),
                },
            )
            call.status = ToolCallStatus.FAILED
            call.error_message = (
                exc.message if isinstance(exc, AppError) else "Ошибка чтения пакета знаний."
            )
            call.completed_at = datetime.now(UTC)
            await session.commit()
            raise


tool_registry.register("read_knowledge_pack", read_knowledge_pack)
