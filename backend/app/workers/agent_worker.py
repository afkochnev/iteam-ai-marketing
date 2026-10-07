import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import create_worker_session_factory
from app.core.error_monitoring import report_exception
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import AgentRunnerService, AgentRuntimeError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


@celery_app.task(name="execute_agent_run")  # type: ignore[misc]
def execute_agent_run(agent_run_id: str) -> None:
    asyncio.run(_run_in_worker_loop(UUID(agent_run_id)))


async def _run_in_worker_loop(run_id: UUID) -> None:
    worker_engine, factory = create_worker_session_factory()
    try:
        await _execute(run_id, factory)
    finally:
        await worker_engine.dispose()


async def _execute(run_id: UUID, factory: SessionFactory | None = None) -> None:
    if factory is None:
        from app.core.database import async_session_factory

        factory = async_session_factory
    async with factory() as session:
        claimed = await AgentRunService(session, factory).claim(run_id)
    if claimed is None:
        return
    snapshot, task_input, context, trace_id = claimed
    try:
        result = await AgentRunnerService().run(snapshot, task_input, context, trace_id)
    except AgentRuntimeError as error:
        logger.exception(
            "Agent run failed",
            extra={"agent_run_id": str(run_id), "error_code": error.code},
        )
        async with factory() as session:
            await AgentRunService(session, factory).finish_failure(run_id, error)
        return
    except Exception as error:
        logger.exception("Unexpected agent run failure", extra={"agent_run_id": str(run_id)})
        report_exception(error, agent_run_id=str(run_id))
        runtime_error = AgentRuntimeError(
            "AGENT_RUNTIME_ERROR", "Неожиданная ошибка выполнения агента."
        )
        async with factory() as session:
            await AgentRunService(session, factory).finish_failure(
                run_id,
                runtime_error,
            )
        return
    try:
        async with factory() as session:
            await AgentRunService(session, factory).finish_success(run_id, result)
    except Exception as error:
        logger.exception("Agent result persistence failed", extra={"agent_run_id": str(run_id)})
        report_exception(error, agent_run_id=str(run_id), phase="result_persistence")
        runtime_error = AgentRuntimeError(
            "AGENT_RESULT_PROCESSING_FAILED", "Не удалось сохранить результат агента."
        )
        async with factory() as session:
            await AgentRunService(session, factory).finish_failure(run_id, runtime_error)
