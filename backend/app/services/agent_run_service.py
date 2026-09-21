import hashlib
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.factory import AgentRuntimeContext, AgentSnapshot
from app.agents.output_registry import output_type_registry
from app.agents.tool_registry import tool_registry
from app.core.config import settings
from app.core.errors import AppError
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import CampaignStatus
from app.models.task import Task, TaskStatus, TaskType
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_runner_service import AgentRuntimeError, RuntimeResult
from app.services.task_result_processors import result_processor_registry
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


class AgentRunService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = AgentRunRepository(session)

    async def create_queued_run(self, task_id: UUID, *, retry: bool = False) -> AgentRun:
        query = (
            select(Task)
            .options(
                selectinload(Task.campaign),
                selectinload(Task.assigned_agent).selectinload(Agent.tools),
            )
            .where(Task.id == task_id)
            .with_for_update()
        )
        task = (await self.session.execute(query)).scalar_one_or_none()
        if task is None:
            raise AppError("TASK_NOT_FOUND", "Задача не найдена.", 404)
        if task.campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        if task.task_type not in {TaskType.MANUAL, TaskType.CAMPAIGN_PLANNING}:
            raise AppError(
                "TASK_TYPE_NOT_EXECUTABLE", "Этот тип задачи пока нельзя выполнять через AI.", 409
            )
        if retry:
            if task.status is not TaskStatus.FAILED:
                raise AppError(
                    "INVALID_TASK_TRANSITION", "Повторить можно только задачу с ошибкой.", 409
                )
            if task.retry_count >= settings.agent_max_retries:
                raise AppError(
                    "TASK_RETRY_LIMIT_REACHED", "Достигнут лимит повторных запусков.", 409
                )
            dependencies = [
                await TaskService(self.session).get_task(item)
                for item in await TaskRepository(self.session).dependency_ids(task.id)
            ]
            if not all(item.status is TaskStatus.COMPLETED for item in dependencies):
                raise AppError("TASK_NOT_READY", "Зависимости задачи ещё не завершены.", 409)
            task.retry_count += 1
            task.status = TaskStatus.READY
            task.error_message = None
            task.started_at = None
            task.completed_at = None
        elif task.status is not TaskStatus.READY:
            raise AppError("TASK_NOT_READY", "Запустить можно только готовую задачу.", 409)
        agent = task.assigned_agent
        if agent is None:
            raise AppError("TASK_AGENT_NOT_ASSIGNED", "Задаче не назначен агент.", 409)
        if agent.status is not AgentStatus.ACTIVE:
            raise AppError("AGENT_INACTIVE", "Назначенный агент неактивен.", 409)
        if task.task_type is TaskType.CAMPAIGN_PLANNING and agent.slug != "marketing_director":
            raise AppError(
                "INVALID_AGENT_FOR_TASK_TYPE",
                "Планирование кампании может выполнять только Marketing Director.",
                409,
            )
        model = agent.model or settings.openai_default_model
        if not model:
            raise AppError("AGENT_MODEL_NOT_CONFIGURED", "Модель агента не настроена.", 409)
        if await self.repository.get_active_for_task(task.id):
            raise AppError(
                "TASK_ALREADY_QUEUED_OR_RUNNING",
                "Задача уже поставлена в очередь или выполняется.",
                409,
            )
        enabled = [item.tool_name for item in agent.tools if item.is_enabled]
        missing = tool_registry.missing(enabled)
        if missing:
            logger.info(
                "Configured agent tools are not implemented",
                extra={"agent_id": str(agent.id), "missing_tools": missing},
            )
        runtime_input = build_task_input(task)
        try:
            run = await self.repository.create(
                {
                    "agent_id": agent.id,
                    "task_id": task.id,
                    "campaign_id": task.campaign_id,
                    "status": AgentRunStatus.QUEUED,
                    "input_data": {"text": runtime_input},
                    "model": model,
                    "prompt_snapshot": agent.system_prompt,
                    "prompt_hash": hashlib.sha256(agent.system_prompt.encode()).hexdigest(),
                }
            )
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise AppError(
                "TASK_ALREADY_QUEUED_OR_RUNNING",
                "Задача уже поставлена в очередь или выполняется.",
                409,
            ) from exc
        return await self.get_run(run.id)

    async def enqueue(self, run: AgentRun) -> AgentRun:
        try:
            from app.workers.agent_worker import execute_agent_run

            result = execute_agent_run.delay(str(run.id))
            await self.repository.update(run, {"queue_job_id": result.id})
            await self.session.commit()
        except Exception as exc:
            logger.exception("Agent run enqueue failed")
            await self.repository.update(
                run,
                {
                    "status": AgentRunStatus.FAILED,
                    "error_code": "QUEUE_ENQUEUE_FAILED",
                    "error_message": "Не удалось поставить AI-запуск в очередь.",
                    "completed_at": datetime.now(UTC),
                },
            )
            await self.session.commit()
            raise AppError(
                "QUEUE_ENQUEUE_FAILED", "Не удалось поставить AI-запуск в очередь.", 503
            ) from exc
        return await self.get_run(run.id)

    async def get_run(self, run_id: UUID) -> AgentRun:
        run = await self.repository.get_by_id(run_id)
        if run is None:
            raise AppError("AGENT_RUN_NOT_FOUND", "Запуск агента не найден.", 404)
        return run

    async def list_runs(self, **filters: object) -> list[AgentRun]:
        return await self.repository.list_runs(**filters)  # type: ignore[arg-type]

    async def claim(
        self, run_id: UUID
    ) -> tuple[AgentSnapshot, str, AgentRuntimeContext, str | None] | None:
        run = await self.repository.get_by_id(run_id, lock=True)
        if run is None or run.status is not AgentRunStatus.QUEUED:
            return None
        task = await self.session.get(
            Task, run.task_id, options=[selectinload(Task.assigned_agent).selectinload(Agent.tools)]
        )
        if task is None or task.status is not TaskStatus.READY:
            return None
        now = datetime.now(UTC)
        trace_id = None if settings.openai_agents_disable_tracing else f"trace_{uuid4().hex}"
        run.status = AgentRunStatus.RUNNING
        run.started_at = now
        run.trace_id = trace_id
        task.status = TaskStatus.IN_PROGRESS
        task.started_at = now
        agent = task.assigned_agent
        assert agent is not None
        enabled = [item.tool_name for item in agent.tools if item.is_enabled]
        snapshot = AgentSnapshot(
            agent.name,
            run.prompt_snapshot,
            run.model,
            enabled,
            output_type_registry.get(task.task_type),
        )
        await self.session.commit()
        return (
            snapshot,
            str(run.input_data["text"]),
            AgentRuntimeContext(run.agent_id, run.task_id, run.campaign_id, run.id, task.task_type),
            trace_id,
        )

    async def finish_success(self, run_id: UUID, result: RuntimeResult) -> None:
        run = await self.repository.get_by_id(run_id, lock=True)
        if run is None or run.status is not AgentRunStatus.RUNNING:
            return
        task = await self.session.get(Task, run.task_id, with_for_update=True)
        if task is None:
            return
        values = {
            "output_data": result.output_data,
            "request_count": result.request_count,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.total_tokens,
            "trace_id": result.trace_id,
            "openai_response_id": result.openai_response_id,
            "completed_at": datetime.now(UTC),
        }
        if task.status is TaskStatus.CANCELLED:
            values["status"] = AgentRunStatus.CANCELLED
            await self.repository.update(run, values)
            await self.session.commit()
            return
        values["status"] = AgentRunStatus.COMPLETED
        await self.repository.update(run, values)
        try:
            await result_processor_registry.get(task.task_type).process(
                self.session, run, task, result.output_data
            )
            await self.session.commit()
        except AppError as exc:
            await self.session.rollback()
            await self.finish_failure(run_id, AgentRuntimeError(exc.code, exc.message))

    async def finish_failure(self, run_id: UUID, error: AgentRuntimeError) -> None:
        run = await self.repository.get_by_id(run_id, lock=True)
        if run is None or run.status is not AgentRunStatus.RUNNING:
            return
        task = await self.session.get(Task, run.task_id, with_for_update=True)
        now = datetime.now(UTC)
        if task and task.status is TaskStatus.CANCELLED:
            run.status = AgentRunStatus.CANCELLED
            run.completed_at = now
        else:
            run.status = AgentRunStatus.FAILED
            run.error_code = error.code
            run.error_message = str(error)
            run.completed_at = now
            if task:
                task.status = TaskStatus.FAILED
                task.error_message = str(error)
                task.completed_at = now
        await self.session.commit()


def build_task_input(task: Task) -> str:
    campaign = task.campaign
    revision_context = ""
    if task.task_type is TaskType.CAMPAIGN_PLANNING:
        revision_context = f"""
Версия стратегии: {task.input_data.get("strategy_version")}
Предыдущая стратегия: {task.input_data.get("previous_strategy", "Нет")}
Комментарий человека: {task.input_data.get("reviewer_feedback", "Нет")}
Период: {campaign.start_date or "Не указан"} — {campaign.end_date or "Не указан"}
Оффер: {campaign.offer or "Не указан"}
Желаемый результат: {campaign.desired_result or "Не указан"}
Контекст кампании: {campaign.description or "Не указан"}
"""
    return f"""Выполни следующую задачу.

Кампания: {campaign.name}
Цель кампании: {campaign.goal}
Продукт: {campaign.product or "Не указан"}
Целевая аудитория: {campaign.target_audience or "Не указана"}

Задача: {task.title}
Тип: {task.task_type.value}
Описание: {task.description or "Не указано"}
Дополнительные входные данные: {task.input_data}
{revision_context}"""
