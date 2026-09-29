import hashlib
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
from app.models.content import ContentItem, ContentStatus, ContentType, ContentVersion

logger = logging.getLogger(__name__)


@function_tool
async def read_content_version(
    ctx: RunContextWrapper[AgentRuntimeContext], content_version_id: UUID
) -> str:
    runtime = ctx.context
    if content_version_id not in runtime.allowed_content_version_ids:
        raise AppError(
            "CONTENT_VERSION_ACCESS_DENIED", "Версия контента недоступна этому запуску.", 403
        )
    cached = runtime.content_version_cache.get(str(content_version_id))
    if cached is not None:
        return cached
    factory = runtime.session_factory or async_session_factory
    started = monotonic()
    logger.info(
        "Agent tool started",
        extra={
            "event": "agent_tool_started",
            "tool_name": "read_content_version",
            "agent_run_id": str(runtime.agent_run_id),
            "task_id": str(runtime.task_id),
        },
    )
    async with factory() as session:
        call = ToolCall(
            agent_run_id=runtime.agent_run_id,
            tool_name="read_content_version",
            arguments={"content_version_id": str(content_version_id)},
            status=ToolCallStatus.STARTED,
            started_at=datetime.now(UTC),
        )
        session.add(call)
        await session.commit()
        try:
            row = (
                await session.execute(
                    select(ContentVersion, ContentItem)
                    .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
                    .where(
                        ContentVersion.id == content_version_id,
                        ContentItem.content_type == ContentType.ARTICLE,
                        ContentItem.status != ContentStatus.ARCHIVED,
                    )
                )
            ).one_or_none()
            if row is None:
                raise AppError("CONTENT_VERSION_NOT_FOUND", "Версия статьи не найдена.", 404)
            version, item = row
            structured = version.structured_content
            result = {
                "content_item_id": str(item.id),
                "content_version_id": str(version.id),
                "title": item.title,
                "article": {
                    "lead": structured.get("lead", ""),
                    "sections": [
                        {
                            "key": section.get("key"),
                            "heading": section.get("heading"),
                            "body_markdown": section.get("body_markdown"),
                        }
                        for section in structured.get("sections", [])
                    ],
                    "conclusion": structured.get("conclusion", ""),
                    "cta": structured.get("cta", ""),
                },
            }
            call.result = {
                "content_version_id": str(version.id),
                "title": item.title,
                "section_keys": [section.get("key") for section in structured.get("sections", [])],
                "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
            }
            call.status = ToolCallStatus.COMPLETED
            call.completed_at = datetime.now(UTC)
            await session.commit()
            runtime.content_version_cache[str(content_version_id)] = json.dumps(
                result, ensure_ascii=False
            )
            logger.info(
                "Agent tool completed",
                extra={
                    "event": "agent_tool_completed",
                    "tool_name": "read_content_version",
                    "agent_run_id": str(runtime.agent_run_id),
                    "task_id": str(runtime.task_id),
                    "elapsed_ms": round((monotonic() - started) * 1000, 2),
                },
            )
            return runtime.content_version_cache[str(content_version_id)]
        except Exception as exc:
            logger.exception(
                "Agent tool failed",
                extra={
                    "event": "agent_tool_failed",
                    "tool_name": "read_content_version",
                    "agent_run_id": str(runtime.agent_run_id),
                    "task_id": str(runtime.task_id),
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": round((monotonic() - started) * 1000, 2),
                },
            )
            call.status = ToolCallStatus.FAILED
            call.error_message = (
                exc.message if isinstance(exc, AppError) else "Ошибка чтения статьи."
            )
            call.completed_at = datetime.now(UTC)
            await session.commit()
            raise


tool_registry.register("read_content_version", read_content_version)
