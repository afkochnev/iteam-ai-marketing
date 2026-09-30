import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agents import Agent, OpenAIResponsesModel, RunConfig, Runner
from openai import AsyncOpenAI
from sqlalchemy import select

from app.core.config import settings
from app.core.database import create_worker_session_factory
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.content import ContentItem, ContentStatus, ContentType, ContentVersion
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus
from app.schemas.publication_plan import PublicationPlanAgentResult
from app.services.activity_log_service import ActivityLogService
from app.workers.celery_app import celery_app

_ID_FRAGMENT = re.compile(
    r"(?:материал|статья)?\s*(?:версии|version)?\s*[0-9a-f]{8}(?:-[0-9a-f]{4})?",
    re.I,
)
_VAGUE_ANGLES = {
    "ключевой тезис",
    "ключевой тезис материала",
    "дополнительный аспект",
    "практический ракурс",
    "объясняющий ракурс",
    "фокус на деталях",
    "основные выводы",
    "итоговый акцент",
    "итоговые выводы",
    "завершение цикла",
    "контекст материала",
}
_ENGAGEMENT_PURPOSES = {
    "охват",
    "вовлечение",
    "комментарии",
    "реакции",
    "сохранения",
    "повторный охват",
    "удержание внимания",
    "закрепление",
    "глубина взаимодействия",
    "ценность для аудитории",
}
_VAGUE_TOPICS = {"материал", "статья", "обзор статьи", "анонс статьи"}


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _editorial_validation_errors(item: Any) -> list[str]:
    fields = (item.topic, item.angle, item.purpose, item.message_brief)
    errors: list[str] = []
    if any(_ID_FRAGMENT.search(value) for value in fields):
        errors.append("EDITORIAL_INTERNAL_ID")
    topic, angle, purpose = map(_norm, (item.topic, item.angle, item.purpose))
    if topic in _VAGUE_TOPICS or topic.startswith("материал версии"):
        errors.append("EDITORIAL_TOPIC_TOO_GENERIC")
    if angle in _VAGUE_ANGLES:
        errors.append("EDITORIAL_ANGLE_TOO_GENERIC")
    if purpose in _ENGAGEMENT_PURPOSES:
        errors.append("EDITORIAL_PURPOSE_NOT_MANAGEMENT_OUTCOME")
    if not item.message_brief.strip() or len(item.message_brief.strip()) < 20:
        errors.append("EDITORIAL_MESSAGE_BRIEF_TOO_SHORT")
    if len({topic, angle, purpose}) < 3:
        errors.append("EDITORIAL_FIELDS_NOT_DISTINCT")
    return errors


@celery_app.task(name="generate_publication_plan")  # type: ignore[misc]
def generate_publication_plan(agent_run_id: str) -> None:
    asyncio.run(_run(UUID(agent_run_id)))


def _build_repair_prompt(snapshot: dict[str, Any], errors: list[str], previous: object) -> str:
    return (
        "Исправь только перечисленные ошибки в предыдущем результате. "
        "Верни только структурированный план, используя тот же снимок, "
        "разрешённые версии, claim IDs, каналы и горизонт. Каждый пункт "
        "обязан ссылаться на claims своего exact source version. Ошибки: "
        f"{errors}. Предыдущий результат: {previous}. "
        f"Frozen input: {snapshot}"
    )


async def _planner_validation_errors(
    session: Any,
    output: PublicationPlanAgentResult,
    snapshot: dict[str, Any],
    campaign_id: UUID,
) -> list[str]:
    errors: list[str] = []
    allowed_versions = set(snapshot["article_version_ids"])
    start = datetime.fromisoformat(snapshot["planning_horizon_start"])
    end = datetime.fromisoformat(snapshot["planning_horizon_end"])
    allowed_channels = set(snapshot["channels"])
    digests = {
        digest["source_content_version_id"]: digest
        for digest in snapshot.get("article_digests", [])
    }
    requested_items = snapshot.get("total_items")
    if requested_items is not None and len(output.items) != requested_items:
        errors.append("ITEM_COUNT_INVALID")
    seen_slots: set[tuple[datetime, str]] = set()
    for item in output.items:
        try:
            scheduled = item.scheduled_at.astimezone(UTC)
        except (AttributeError, ValueError):
            errors.append("SCHEDULE_NOT_TIMEZONE_AWARE")
            continue
        if scheduled < start.astimezone(UTC) or scheduled > end.astimezone(UTC):
            errors.append("PLAN_ITEM_OUTSIDE_HORIZON")
        if item.channel.value not in allowed_channels:
            errors.append("PLAN_CHANNEL_INVALID")
        errors.extend(_editorial_validation_errors(item))
        if not item.source_claim_ids:
            errors.append("PLAN_SOURCE_CLAIMS_REQUIRED")
        slot = (scheduled, item.channel.value)
        if slot in seen_slots:
            errors.append("PLAN_DUPLICATE_SLOT")
        seen_slots.add(slot)
        if str(item.source_content_version_id) not in allowed_versions:
            errors.append("PLAN_SOURCE_NOT_IN_FROZEN_INPUT")
            continue
        version = await session.get(ContentVersion, item.source_content_version_id)
        source_item = await session.get(ContentItem, version.content_item_id) if version else None
        if (
            version is None
            or source_item is None
            or source_item.content_type is not ContentType.ARTICLE
            or source_item.status is not ContentStatus.APPROVED
            or source_item.campaign_id != campaign_id
        ):
            errors.append("PLAN_SOURCE_INVALID")
        digest = digests.get(str(item.source_content_version_id))
        allowed_claims = {claim["claim_id"] for claim in (digest or {}).get("allowed_claims", [])}
        if digest is None:
            errors.append("PLAN_SOURCE_DIGEST_MISSING")
        elif any(claim_id not in allowed_claims for claim_id in item.source_claim_ids):
            errors.append("PLAN_SOURCE_CLAIM_INVALID")
    return list(dict.fromkeys(errors))


async def _run(agent_run_id: UUID) -> None:
    engine, factory = create_worker_session_factory()
    try:
        async with factory() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == agent_run_id).with_for_update()
            )
            if run is None or run.status != AgentRunStatus.QUEUED:
                return
            run.status = AgentRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            plan = await session.scalar(
                select(PublicationPlan)
                .where(PublicationPlan.generated_by_agent_run_id == run.id)
                .with_for_update()
            )
            if plan is None:
                raise RuntimeError("publication plan artifact is missing")
            await ActivityLogService(session).record(
                "PUBLICATION_PLAN_GENERATION_STARTED",
                operation_key=f"publication-plan-started:{run.id}",
                campaign_id=run.campaign_id,
                metadata={"plan_id": str(plan.id), "agent_run_id": str(run.id)},
            )
            await session.commit()
            snapshot = run.input_data["publication_plan_snapshot"]
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_NOT_CONFIGURED")
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.agent_provider_request_timeout_seconds,
            max_retries=settings.agent_provider_max_retries,
        )
        repair_attempted = False
        validation_errors: list[str] = []
        previous_output: object = "<unavailable>"
        try:
            agent = Agent(
                name="Publication Planner",
                instructions=(
                    "Предложи только структурированный редакционный план. "
                    "Используй только exact article_version_ids из снимка; "
                    "не создавай тексты постов и не придумывай источники. "
                    "Планируй конкретные управленческие идеи по смыслу статей, "
                    "а не анонсы и не пересказы. Никогда не используй ID версий "
                    "в topic, angle, purpose или message_brief. topic должен "
                    "называть реальную управленческую проблему; angle — объяснять "
                    "редакционный ракурс; purpose — управленческий результат для "
                    "читателя, а не охват или вовлечение. message_brief должен "
                    "содержать ситуацию, центральную мысль и вывод. Форматы — "
                    "содержательные редакционные конструкции. Для каждого пункта "
                    "укажи 1–3 source_claim_ids только из digest exact source version "
                    "и source_support_summary, объясняющий связь с claim. Не добавляй "
                    "смежные идеи, которых нет в claims."
                ),
                model=run.model,
                output_type=PublicationPlanAgentResult,
            )
            for attempt in range(2):
                repair_attempted = attempt == 1
                if repair_attempted:
                    prompt = _build_repair_prompt(snapshot, validation_errors, previous_output)
                else:
                    prompt = f"Снимок плана: {snapshot}"
                try:
                    result = await Runner.run(
                        agent,
                        prompt,
                        run_config=RunConfig(
                            model=OpenAIResponsesModel(run.model, client),
                            tracing_disabled=settings.openai_agents_disable_tracing,
                        ),
                    )
                    output = PublicationPlanAgentResult.model_validate(result.final_output)
                except Exception as exc:
                    validation_errors = ["STRUCTURED_OUTPUT_INVALID"]
                    if attempt == 0:
                        previous_output = "<unavailable>"
                        continue
                    raise RuntimeError("PLAN_REPAIR_EXHAUSTED") from exc
                async with factory() as validation_session:
                    validation_errors = await _planner_validation_errors(
                        validation_session,
                        output,
                        snapshot,
                        run.campaign_id,
                    )
                if not validation_errors:
                    break
                previous_output = output.model_dump(mode="json")
                if attempt == 0:
                    async with factory() as audit_session:
                        await ActivityLogService(audit_session).record(
                            "PUBLICATION_PLAN_GENERATION_REPAIR_ATTEMPTED",
                            operation_key=f"publication-plan-repair:{run.id}",
                            campaign_id=run.campaign_id,
                            metadata={
                                "agent_run_id": str(run.id),
                                "validation_errors": validation_errors,
                            },
                        )
                        await audit_session.commit()
                    continue
                raise RuntimeError("PLAN_REPAIR_EXHAUSTED")
        finally:
            await client.close()
        async with factory() as session:
            run = await session.get(AgentRun, agent_run_id, with_for_update=True)
            plan = await session.scalar(
                select(PublicationPlan)
                .where(PublicationPlan.generated_by_agent_run_id == agent_run_id)
                .with_for_update()
            )
            if run is None or plan is None or run.status != AgentRunStatus.RUNNING:
                return
            snapshot = run.input_data["publication_plan_snapshot"]
            validation_errors = await _planner_validation_errors(
                session, output, snapshot, run.campaign_id
            )
            if validation_errors:
                raise RuntimeError("PLAN_REPAIR_EXHAUSTED")
            for position, item in enumerate(output.items, start=1):
                version = await session.get(ContentVersion, item.source_content_version_id)
                if version is None:
                    raise RuntimeError("PLAN_SOURCE_INVALID")
                session.add(
                    PublicationPlanItem(
                        publication_plan_id=plan.id,
                        position=position,
                        scheduled_at=item.scheduled_at.astimezone(UTC),
                        channel=item.channel,
                        source_content_item_id=version.content_item_id,
                        source_content_version_id=version.id,
                        topic=item.topic,
                        angle=item.angle,
                        purpose=item.purpose,
                        format=item.format,
                        message_brief=item.message_brief,
                        source_claim_ids=item.source_claim_ids,
                        source_support_summary=item.source_support_summary,
                        status=PublicationPlanItemStatus.PLANNED,
                    )
                )
            plan.status = PublicationPlanStatus.WAITING_APPROVAL
            run.status = AgentRunStatus.COMPLETED
            run.completed_at = datetime.now(UTC)
            run.output_data = {
                "plan_id": str(plan.id),
                "item_count": len(output.items),
                "repair_attempted": repair_attempted,
                "validation_errors": validation_errors,
            }
            task = await session.get(Task, run.task_id, with_for_update=True)
            if task is not None:
                task.status = TaskStatus.COMPLETED
                task.completed_at = datetime.now(UTC)
                task.output_data = {"plan_id": str(plan.id)}
            await ActivityLogService(session).record(
                "PUBLICATION_PLAN_GENERATED",
                operation_key=f"publication-plan-generated:{run.id}",
                campaign_id=run.campaign_id,
                metadata={
                    "plan_id": str(plan.id),
                    "agent_run_id": str(run.id),
                    "item_count": len(output.items),
                },
            )
            await session.commit()
    except Exception as exc:
        async with factory() as session:
            run = await session.get(AgentRun, agent_run_id, with_for_update=True)
            if run is not None and run.status not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
            }:
                run.status = AgentRunStatus.FAILED
                run.error_code = "PUBLICATION_PLAN_GENERATION_FAILED"
                run.error_message = "Не удалось сформировать план публикаций."
                run.completed_at = datetime.now(UTC)
                plan = await session.scalar(
                    select(PublicationPlan)
                    .where(PublicationPlan.generated_by_agent_run_id == run.id)
                    .with_for_update()
                )
                if plan is not None:
                    plan.status = PublicationPlanStatus.DRAFT
                task = await session.get(Task, run.task_id, with_for_update=True)
                if task is not None:
                    task.status = TaskStatus.FAILED
                    task.error_message = run.error_message
                await ActivityLogService(session).record(
                    "PUBLICATION_PLAN_GENERATION_FAILED",
                    operation_key=f"publication-plan-failed:{run.id}",
                    campaign_id=run.campaign_id,
                    metadata={
                        "plan_id": str(plan.id) if plan else None,
                        "agent_run_id": str(run.id),
                    },
                )
                await session.commit()
        raise exc
    finally:
        await engine.dispose()
