import asyncio
from datetime import UTC, datetime
from uuid import UUID

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner
from agents.exceptions import ModelBehaviorError
from openai import AsyncOpenAI
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.core.database import create_worker_session_factory
from app.core.errors import AppError
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.marketing_feedback import (
    FeedbackAnalysisStatus,
    MarketingFeedbackAnalysis,
)
from app.models.task import Task, TaskStatus
from app.schemas.feedback import FeedbackAnalystResult
from app.services.activity_log_service import ActivityLogService
from app.services.feedback_service import FeedbackService
from app.workers.celery_app import celery_app


@celery_app.task(name="generate_feedback_analysis")  # type: ignore[misc]
def generate_feedback_analysis(agent_run_id: str) -> None:
    asyncio.run(_run(UUID(agent_run_id)))


async def _run(agent_run_id: UUID) -> None:
    engine, factory = create_worker_session_factory()
    try:
        async with factory() as session:
            run = (
                await session.execute(
                    select(AgentRun).where(AgentRun.id == agent_run_id).with_for_update()
                )
            ).scalar_one_or_none()
            if run is None or run.status is not AgentRunStatus.QUEUED:
                return
            run.status = AgentRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            analysis_id = await session.scalar(
                select(MarketingFeedbackAnalysis.id).where(
                    MarketingFeedbackAnalysis.agent_run_id == run.id
                )
            )
            if analysis_id is None:
                raise RuntimeError("feedback analysis artifact is missing")
            await ActivityLogService(session).record(
                "FEEDBACK_ANALYSIS_STARTED",
                operation_key=f"feedback-analysis-started:{run.id}",
                campaign_id=run.campaign_id,
                metadata={"analysis_id": str(analysis_id), "agent_run_id": str(run.id)},
            )
            await session.commit()
        async with factory() as session:
            run = await session.get(AgentRun, agent_run_id)
            if run is None:
                return
            snapshot = run.input_data["feedback_snapshot"]
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_NOT_CONFIGURED")
        prompt = (
            "Проанализируй только зафиксированный снимок обратной связи. "
            "Разделяй факты, интерпретации и гипотезы; ссылайся только на evidence_refs "
            "из снимка; сохраняй MANUAL/PROVIDER и NULL/0; показывай противоречия; "
            "не выдумывай причинность, ROI, клиентов или результаты; не создавай общий score.\n"
            f"Снимок: {snapshot}"
        )
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.agent_provider_request_timeout_seconds,
            max_retries=settings.agent_provider_max_retries,
        )
        typed_result: FeedbackAnalystResult | None = None
        repair_attempt_count = 0
        validation_error = ""
        try:
            analyst = Agent(
                name="Feedback Analyst",
                instructions=(
                    "Выдавай только строгий FeedbackAnalystResult. Рекомендации консультативны."
                ),
                model=run.model,
                output_type=FeedbackAnalystResult,
            )
            model = OpenAIResponsesModel(run.model, client)
            for attempt in range(2):
                repair_attempt_count = attempt
                repair_prompt = prompt
                if attempt:
                    repair_prompt = (
                        f"{prompt}\nИсправь предыдущий результат строго по ошибкам: "
                        f"{validation_error}. Верни только полный исправленный "
                        "FeedbackAnalystResult, используй только тот же снимок и evidence_refs."
                    )
                try:
                    result = await Runner.run(
                        analyst,
                        repair_prompt,
                        run_config=RunConfig(
                            model=model,
                            tracing_disabled=settings.openai_agents_disable_tracing,
                        ),
                    )
                    candidate = FeedbackAnalystResult.model_validate(result.final_output)
                    FeedbackService.validate_evidence(snapshot, candidate)
                    typed_result = candidate
                    break
                except (AppError, ModelBehaviorError, ValidationError) as exc:
                    validation_error = str(exc)[:500] or "structured result validation failed"
                    if attempt == 1:
                        raise RuntimeError("FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED") from exc
            if typed_result is None:
                raise RuntimeError("FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED")
        finally:
            await client.close()
        async with factory() as session:
            assert typed_result is not None
            completed_analysis = await FeedbackService(session).complete_with_result(
                analysis_id, agent_run_id, typed_result
            )
            run = await session.get(AgentRun, agent_run_id, with_for_update=True)
            if run is None:
                return
            run.status = AgentRunStatus.COMPLETED
            run.completed_at = datetime.now(UTC)
            run.output_data = {
                "analysis_id": str(completed_analysis.id),
                "initial_attempt_count": 1,
                "repair_attempt_count": repair_attempt_count,
                "final_validation": "success",
            }
            task = await session.get(Task, run.task_id, with_for_update=True)
            if task is not None:
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now(UTC)
                task.output_data = {"analysis_id": str(completed_analysis.id)}
            await session.commit()
    except Exception as error:
        async with factory() as session:
            run = await session.get(AgentRun, agent_run_id, with_for_update=True)
            if run is not None:
                run.status = AgentRunStatus.FAILED
                run.error_code = (
                    "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED"
                    if "FEEDBACK_ANALYSIS_REPAIR_EXHAUSTED" in str(error)
                    else "FEEDBACK_ANALYSIS_FAILED"
                )
                run.error_message = "Не удалось сформировать анализ обратной связи."
                run.completed_at = datetime.now(UTC)
                task = await session.get(Task, run.task_id, with_for_update=True)
                if task is not None:
                    task.status = TaskStatus.FAILED
                    task.completed_at = datetime.now(UTC)
                    task.error_message = run.error_message
                analysis = await session.scalar(
                    select(MarketingFeedbackAnalysis).where(
                        MarketingFeedbackAnalysis.agent_run_id == run.id
                    )
                )
                if analysis is not None:
                    analysis.status = FeedbackAnalysisStatus.FAILED
                    run.output_data = {
                        "analysis_id": str(analysis.id),
                        "initial_attempt_count": 1,
                        "repair_attempt_count": 1 if "REPAIR_EXHAUSTED" in str(error) else 0,
                        "final_validation": "failed",
                    }
                    await ActivityLogService(session).record(
                        "FEEDBACK_ANALYSIS_FAILED",
                        operation_key=f"feedback-analysis-failed:{run.id}",
                        campaign_id=run.campaign_id,
                        metadata={
                            "analysis_id": str(analysis.id),
                            "agent_run_id": str(run.id),
                        },
                    )
                await session.commit()
        raise error
    finally:
        await engine.dispose()
