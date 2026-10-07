from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select, text

from app.api.dependencies import AdminUser, SessionDependency
from app.core.config import settings
from app.core.database import engine
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.approval import Approval, ApprovalStatus
from app.models.marketing_feedback import MarketingFeedbackAnalysis
from app.models.publication import Publication, PublicationStatus
from app.models.task import Task, TaskStatus, TaskType
from app.services.reconciliation_integrity import reconciliation_integrity_report

EXPECTED_MIGRATION_HEAD = "20261007_0022"

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def system_status(_admin: AdminUser, session: SessionDependency) -> dict[str, Any]:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.task_stuck_after_seconds)
    publication_cutoff = now - timedelta(
        seconds=settings.publication_auto_dispatch_max_lateness_seconds
    )
    task_counts = {
        status.value: int(
            await session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.status == status, Task.is_internal.is_(False))
            )
            or 0
        )
        for status in (TaskStatus.READY, TaskStatus.IN_PROGRESS, TaskStatus.FAILED)
    }
    run_counts = {
        status.value: int(
            await session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.status == status)
            )
            or 0
        )
        for status in (AgentRunStatus.RUNNING, AgentRunStatus.FAILED)
    }
    publication_counts = {
        "reconciliation_required": int(
            await session.scalar(
                select(func.count())
                .select_from(Publication)
                .where(
                    Publication.status == PublicationStatus.FAILED,
                    Publication.failure_code.in_(
                        ["TELEGRAM_RECONCILIATION_REQUIRED", "VK_RECONCILIATION_REQUIRED"]
                    ),
                )
            )
            or 0
        ),
        "failed": int(
            await session.scalar(
                select(func.count())
                .select_from(Publication)
                .where(Publication.status == PublicationStatus.FAILED)
            )
            or 0
        ),
    }
    scheduled = Publication.status == PublicationStatus.SCHEDULED
    for name, conditions in {
        "scheduled": [scheduled],
        "due": [
            scheduled,
            Publication.scheduled_at >= publication_cutoff,
            Publication.scheduled_at <= now,
        ],
        "overdue": [scheduled, Publication.scheduled_at < publication_cutoff],
        "provider_disabled": [
            scheduled,
            Publication.channel.in_(
                [
                    channel
                    for channel, enabled in [
                        ("TELEGRAM", settings.telegram_publishing_enabled),
                        ("VK", settings.vk_publishing_enabled),
                    ]
                    if not enabled
                ]
            ),
        ],
    }.items():
        publication_counts[name] = int(
            await session.scalar(select(func.count()).select_from(Publication).where(*conditions))
            or 0
        )
    auto_ready = [
        Task.status == TaskStatus.READY,
        Task.is_internal.is_(False),
        Task.task_type.in_(
            [
                TaskType.KNOWLEDGE_RESEARCH,
                TaskType.WRITE_ARTICLE,
                TaskType.CREATE_SOCIAL_POSTS,
                TaskType.CONTENT_REVISION,
            ]
        ),
    ]
    ready_auto_ai = int(
        await session.scalar(select(func.count()).select_from(Task).where(*auto_ready)) or 0
    )
    stalled_ready_auto_ai = int(
        await session.scalar(
            select(func.count()).select_from(Task).where(*auto_ready, Task.updated_at < cutoff)
        )
        or 0
    )
    metrics_failures = int(
        await session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.event_type == "PUBLICATION_METRICS_SYNC_FAILED")
        )
        or 0
    )
    feedback_failures = int(
        await session.scalar(
            select(func.count())
            .select_from(MarketingFeedbackAnalysis)
            .where(MarketingFeedbackAnalysis.status == "FAILED")
        )
        or 0
    )
    reconciliation_integrity = await reconciliation_integrity_report(session)
    stuck = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .join(Task, Task.id == AgentRun.task_id)
        .where(
            AgentRun.status == AgentRunStatus.RUNNING,
            Task.status == TaskStatus.IN_PROGRESS,
            Task.is_internal.is_(False),
            AgentRun.started_at.is_not(None),
            AgentRun.started_at < cutoff,
        )
    )
    pending = await session.scalar(
        select(func.count()).select_from(Approval).where(Approval.status == ApprovalStatus.PENDING)
    )
    latest = await session.scalar(
        select(ActivityLog.created_at).order_by(ActivityLog.created_at.desc()).limit(1)
    )
    return {
        "version": settings.app_version,
        "release_sha": settings.build_sha,
        "build_sha": settings.build_sha,
        "environment": settings.app_env,
        "tasks": task_counts,
        "agent_runs": run_counts,
        "publications": publication_counts,
        "due_publications": publication_counts["due"],
        "overdue_publications": publication_counts["overdue"],
        "scheduled_publications": publication_counts["scheduled"],
        "publishing_providers_enabled": {
            "telegram": settings.telegram_publishing_enabled,
            "vk": settings.vk_publishing_enabled,
        },
        "ready_auto_ai_tasks": ready_auto_ai,
        "stalled_ready_auto_ai_tasks": stalled_ready_auto_ai,
        "running_agent_runs": run_counts["RUNNING"],
        "metrics_sync_failures": metrics_failures,
        "feedback_analysis_failures": feedback_failures,
        "reconciliation_integrity": reconciliation_integrity,
        "stuck_tasks": int(stuck or 0),
        "pending_approvals": int(pending or 0),
        "last_activity_at": latest,
    }


async def _check_database() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url)
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()
    except Exception:
        return False


async def _check_schema() -> bool:
    try:
        async with engine.connect() as connection:
            rows = (
                (await connection.execute(text("SELECT version_num FROM alembic_version")))
                .scalars()
                .all()
            )
        return len(rows) == 1 and rows[0] == EXPECTED_MIGRATION_HEAD
    except Exception:
        return False


@router.get("/ready")
async def readiness() -> dict[str, str]:
    try:
        settings.validate_production()
    except RuntimeError as error:
        raise AppError(
            "READINESS_CONFIG_INVALID",
            "Конфигурация сервиса не позволяет принимать рабочие запросы.",
            503,
        ) from error
    database_ok, redis_ok = await _check_database(), await _check_redis()
    schema_ok = await _check_schema() if database_ok else False
    payload = {
        "status": "ok" if database_ok and redis_ok and schema_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
        "schema": "ok" if schema_ok else "incompatible",
    }
    if not database_ok or not redis_ok or not schema_ok:
        raise AppError(
            "READINESS_CHECK_FAILED",
            "Сервис ещё не готов принимать запросы.",
            503,
            payload,
        )
    return payload
