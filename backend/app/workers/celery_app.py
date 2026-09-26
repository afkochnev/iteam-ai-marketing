from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "iteam_ai_marketing",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.agent_worker",
        "app.workers.knowledge_worker",
        "app.workers.dispatcher_worker",
        "app.workers.recovery_worker",
        "app.workers.telegram_worker",
    ],
)
celery_app.conf.update(task_track_started=True, timezone="UTC", enable_utc=True)
celery_app.conf.beat_schedule = {
    "dispatch-ready-ai-tasks": {
        "task": "dispatch_ready_tasks",
        "schedule": settings.task_dispatch_interval_seconds,
    },
    "recover-stuck-tasks": {
        "task": "recover_stuck_tasks",
        "schedule": settings.task_dispatch_interval_seconds,
    },
    "dispatch-due-publications": {
        "task": "dispatch_due_publications",
        "schedule": settings.task_dispatch_interval_seconds,
    },
}
