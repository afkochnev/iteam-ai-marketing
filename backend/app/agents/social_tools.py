import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from agents import RunContextWrapper, function_tool
from sqlalchemy import select

from app.agents.factory import AgentRuntimeContext
from app.agents.tool_registry import tool_registry
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.agent_run import ToolCall, ToolCallStatus
from app.models.content import ContentItem, ContentType, ContentVersion


@function_tool
async def read_content_version(
    ctx: RunContextWrapper[AgentRuntimeContext], content_version_id: UUID
) -> str:
    runtime = ctx.context
    if content_version_id not in runtime.allowed_content_version_ids:
        raise AppError(
            "CONTENT_VERSION_ACCESS_DENIED", "Версия контента недоступна этому запуску.", 403
        )
    async with async_session_factory() as session:
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
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            call.status = ToolCallStatus.FAILED
            call.error_message = (
                exc.message if isinstance(exc, AppError) else "Ошибка чтения статьи."
            )
            call.completed_at = datetime.now(UTC)
            await session.commit()
            raise


tool_registry.register("read_content_version", read_content_version)
