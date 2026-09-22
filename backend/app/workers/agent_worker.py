import asyncio
import logging
from uuid import UUID

from app.core.database import async_session_factory
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import AgentRunnerService, AgentRuntimeError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="execute_agent_run")  # type: ignore[untyped-decorator]
def execute_agent_run(agent_run_id: str) -> None:
    asyncio.run(_execute(UUID(agent_run_id)))


async def _execute(run_id: UUID) -> None:
    async with async_session_factory() as session:
        claimed = await AgentRunService(session).claim(run_id)
    if claimed is None:
        return
    snapshot, task_input, context, trace_id = claimed
    try:
        result = await AgentRunnerService().run(snapshot, task_input, context, trace_id)
    except AgentRuntimeError as error:
        logger.exception(
            "Agent run failed", extra={"agent_run_id": str(run_id), "error_code": error.code}
        )
        async with async_session_factory() as session:
            await AgentRunService(session).finish_failure(run_id, error)
        return
    except Exception:
        logger.exception("Unexpected agent run failure", extra={"agent_run_id": str(run_id)})
        runtime_error = AgentRuntimeError(
            "AGENT_RUNTIME_ERROR", "Неожиданная ошибка выполнения агента."
        )
        async with async_session_factory() as session:
            await AgentRunService(session).finish_failure(run_id, runtime_error)
        return
    try:
        async with async_session_factory() as session:
            await AgentRunService(session).finish_success(run_id, result)
    except Exception:
        logger.exception("Agent result persistence failed", extra={"agent_run_id": str(run_id)})
        runtime_error = AgentRuntimeError(
            "AGENT_RESULT_PROCESSING_FAILED", "Не удалось сохранить результат агента."
        )
        async with async_session_factory() as session:
            await AgentRunService(session).finish_failure(run_id, runtime_error)
