import json
import logging
from datetime import UTC, datetime

from agents import RunContextWrapper, function_tool

from app.agents.factory import AgentRuntimeContext
from app.agents.tool_registry import tool_registry
from app.core.database import async_session_factory
from app.models.agent_run import ToolCall, ToolCallStatus
from app.services.knowledge_search_service import KnowledgeSearchService

logger = logging.getLogger(__name__)


@function_tool
async def search_knowledge(
    ctx: RunContextWrapper[AgentRuntimeContext], query: str, max_results: int = 10
) -> str:
    """Search the verified iTeam knowledge base and return results with provenance.

    Args:
        query: A precise semantic search query.
        max_results: Number of results to return, from 1 to 20.
    """
    arguments = {"query": query, "max_results": max_results}
    async with async_session_factory() as session:
        audit = ToolCall(
            agent_run_id=ctx.context.agent_run_id,
            tool_name="search_knowledge",
            arguments=arguments,
            status=ToolCallStatus.STARTED,
            started_at=datetime.now(UTC),
        )
        session.add(audit)
        await session.commit()
        audit_id = audit.id
    try:
        async with async_session_factory() as session:
            response = await KnowledgeSearchService(session).search(query, max_results)
        payload = response.model_dump(mode="json")
        async with async_session_factory() as session:
            stored_audit = await session.get(ToolCall, audit_id, with_for_update=True)
            if stored_audit:
                stored_audit.status = ToolCallStatus.COMPLETED
                stored_audit.result = payload
                stored_audit.completed_at = datetime.now(UTC)
                await session.commit()
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        logger.exception(
            "search_knowledge tool failed", extra={"agent_run_id": str(ctx.context.agent_run_id)}
        )
        async with async_session_factory() as session:
            stored_audit = await session.get(ToolCall, audit_id, with_for_update=True)
            if stored_audit:
                stored_audit.status = ToolCallStatus.FAILED
                stored_audit.error_message = "Поиск по базе знаний завершился ошибкой."
                stored_audit.completed_at = datetime.now(UTC)
                await session.commit()
        raise exc


tool_registry.register("search_knowledge", search_knowledge)
