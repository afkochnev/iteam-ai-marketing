"""One specialized, evidence-validating execution path for durable analyst tasks."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from agents import Agent as SDKAgent
from agents import ModelBehaviorError, OpenAIResponsesModel, RunConfig, Runner
from openai import AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.errors import AppError
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.models.task import Task, TaskStatus, TaskType
from app.schemas.feedback import FeedbackAnalystResult
from app.services.feedback_service import FeedbackService
from app.services.performance_analysis_lifecycle import fail_linked_analysis
from app.services.performance_evidence import canonical_json
from app.workers.celery_app import celery_app


@celery_app.task(name="generate_feedback_analysis")  # type: ignore[misc]
def generate_feedback_analysis(run_id: str) -> None:
    asyncio.run(_generate(UUID(run_id)))


async def _generate(run_id: UUID) -> None:
    loop_engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(loop_engine, expire_on_commit=False)
    try:
        async with factory() as session:
            run = await session.get(AgentRun, run_id, with_for_update=True)
            if run is None or run.status is not AgentRunStatus.QUEUED:
                await session.rollback()
                return
            task = await session.get(Task, run.task_id, with_for_update=True)
            analysis = await session.scalar(
                select(MarketingFeedbackAnalysis)
                .where(MarketingFeedbackAnalysis.agent_run_id == run.id)
                .with_for_update()
            )
            agent = await session.get(Agent, run.agent_id)
            if task is None or task.status is not TaskStatus.READY:
                await session.rollback()
                return
            if (
                task.task_type is not TaskType.ANALYZE_PERFORMANCE
                or agent is None
                or agent.status is not AgentStatus.ACTIVE
                or agent.slug != "marketing_analyst"
                or task.assigned_agent_id != agent.id
                or analysis is None
                or analysis.status is not FeedbackAnalysisStatus.DRAFT
                or analysis.task_id != task.id
                or analysis.campaign_id != run.campaign_id
                or task.campaign_id != run.campaign_id
                or str(analysis.id) != run.input_data.get("analysis_id")
                or str(analysis.id) != task.input_data.get("analysis_id")
                or analysis.evidence_fingerprint != run.input_data.get("evidence_fingerprint")
                or analysis.evidence_fingerprint != task.input_data.get("evidence_fingerprint")
                or analysis.input_snapshot != run.input_data.get("feedback_snapshot")
                or run.input_data.get("executable_tools") != []
            ):
                run.status = AgentRunStatus.FAILED
                run.error_code = "ANALYSIS_CLAIM_INVALID"
                run.error_message = "Связь задачи и замороженного анализа недействительна."
                run.completed_at = datetime.now(UTC)
                task.status = TaskStatus.FAILED
                task.completed_at = run.completed_at
                task.error_message = run.error_message
                await fail_linked_analysis(session, run)
                await session.commit()
                return
            run.status = AgentRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            task.status = TaskStatus.IN_PROGRESS
            task.started_at = run.started_at
            snapshot = run.input_data["feedback_snapshot"]
            instructions, model = run.prompt_snapshot, run.model
            await FeedbackService(session).analysis_event(analysis, "FEEDBACK_ANALYSIS_STARTED")
            await session.commit()
        try:
            if not settings.openai_api_key:
                raise AppError("OPENAI_NOT_CONFIGURED", "Модель анализа не настроена.", 503)
            # SDK retries/turns cannot hide requests from our explicit two-request budget.
            client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=settings.agent_provider_request_timeout_seconds,
                max_retries=0,
            )
            try:
                analyst = SDKAgent(
                    name="Marketing Analyst",
                    instructions=instructions,
                    tools=[],
                    output_type=FeedbackAnalystResult,
                )
                prompt = "Проанализируй замороженные данные:\n" + canonical_json(snapshot)
                result = None
                for attempt in range(2):
                    async with factory() as session:
                        run = await session.get(AgentRun, run_id, with_for_update=True)
                        if run is None or run.status is not AgentRunStatus.RUNNING:
                            await session.rollback()
                            return
                        run.request_count = (run.request_count or 0) + 1
                        accounting = dict(run.input_data.get("model_request_accounting") or {})
                        accounting.update(
                            logical_generation_attempt_count=1,
                            external_model_request_count=run.request_count,
                            repair_request_count=attempt,
                            sdk_turn_count=run.request_count,
                            last_model_request_at=datetime.now(UTC).isoformat(),
                        )
                        run.input_data = {**run.input_data, "model_request_accounting": accounting}
                        await session.commit()
                    try:
                        answer = await Runner.run(
                            analyst,
                            prompt,
                            max_turns=1,
                            run_config=RunConfig(
                                model=OpenAIResponsesModel(model=model, openai_client=client),
                                tracing_disabled=True,
                            ),
                        )
                        result = FeedbackAnalystResult.model_validate(answer.final_output)
                        FeedbackService.validate_evidence(snapshot, result)
                        break
                    except (AppError, ModelBehaviorError, ValidationError) as error:
                        if attempt == 1:
                            raise AppError(
                                "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED",
                                "Не удалось подтвердить структурированный результат.",
                                422,
                            ) from error
                        # Never send potentially raw feedback/provider errors back as instructions.
                        prompt += (
                            "\nИсправь формат и ссылки: используй только allowlist evidence "
                            "и корректные индексы findings."
                        )
                assert result is not None
            finally:
                await client.close()
            async with factory() as session:
                run = await session.get(AgentRun, run_id, with_for_update=True)
                if run is None or run.status is not AgentRunStatus.RUNNING:
                    await session.rollback()
                    return
                task = await session.get(Task, run.task_id, with_for_update=True)
                analysis = await session.scalar(
                    select(MarketingFeedbackAnalysis)
                    .where(MarketingFeedbackAnalysis.agent_run_id == run.id)
                    .with_for_update()
                )
                if (
                    task is None
                    or task.status is not TaskStatus.IN_PROGRESS
                    or analysis is None
                    or analysis.status is not FeedbackAnalysisStatus.DRAFT
                ):
                    await session.rollback()
                    return
                await FeedbackService(session).complete_with_result(analysis.id, run.id, result)
                run.status = AgentRunStatus.COMPLETED
                run.completed_at = datetime.now(UTC)
                run.output_data = {
                    "analysis_id": str(analysis.id),
                    "final_validation_state": "COMPLETED_VALIDATED",
                    "model_request_count": run.request_count,
                }
                accounting = dict(run.input_data.get("model_request_accounting") or {})
                accounting["final_validation_state"] = "COMPLETED_VALIDATED"
                run.input_data = {**run.input_data, "model_request_accounting": accounting}
                task.status = TaskStatus.COMPLETED
                task.completed_at = run.completed_at
                task.output_data = {"analysis_id": str(analysis.id)}
                await session.commit()
        except Exception as error:
            async with factory() as session:
                run = await session.get(AgentRun, run_id, with_for_update=True)
                if run is None or run.status is not AgentRunStatus.RUNNING:
                    await session.rollback()
                    return
                run.status = AgentRunStatus.FAILED
                run.error_code = (
                    error.code if isinstance(error, AppError) else "FEEDBACK_ANALYSIS_FAILED"
                )
                run.error_message = (
                    "Не удалось выполнить анализ. Возможен повтор с тем же evidence."
                )
                run.completed_at = datetime.now(UTC)
                run.output_data = {
                    "final_validation_state": run.error_code,
                    "model_request_count": run.request_count,
                }
                accounting = dict(run.input_data.get("model_request_accounting") or {})
                accounting["final_validation_state"] = run.error_code
                run.input_data = {**run.input_data, "model_request_accounting": accounting}
                task = await session.get(Task, run.task_id, with_for_update=True)
                if task is not None:
                    task.status = TaskStatus.FAILED
                    task.completed_at = run.completed_at
                    task.error_message = run.error_message
                await fail_linked_analysis(session, run)
                await session.commit()
            raise
    finally:
        await loop_engine.dispose()
