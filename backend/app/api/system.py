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
from app.models.task import Task, TaskStatus

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
async def system_status(_admin: AdminUser, session: SessionDependency) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds)
    task_counts = {
        status.value: int(
            await session.scalar(
                select(func.count()).select_from(Task).where(Task.status == status)
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
    stuck = await session.scalar(
        select(func.count())
        .select_from(AgentRun)
        .join(Task, Task.id == AgentRun.task_id)
        .where(
            AgentRun.status == AgentRunStatus.RUNNING,
            Task.status == TaskStatus.IN_PROGRESS,
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
        "tasks": task_counts,
        "agent_runs": run_counts,
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


@router.get("/ready")
async def readiness() -> dict[str, str]:
    database_ok, redis_ok = await _check_database(), await _check_redis()
    payload = {
        "status": "ok" if database_ok and redis_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
    }
    if not database_ok or not redis_ok:
        raise AppError(
            "READINESS_CHECK_FAILED", "Сервис ещё не готов принимать запросы.", 503, payload
        )
    return payload
