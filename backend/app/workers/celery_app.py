from celery import Celery

from app.core.config import settings

settings.validate_redis_isolation()
celery_app = Celery(
    "iteam_ai_marketing",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend_url,
    include=[
        "app.workers.agent_worker",
        "app.workers.knowledge_worker",
        "app.workers.dispatcher_worker",
        "app.workers.recovery_worker",
        "app.workers.telegram_worker",
        "app.workers.vk_worker",
        "app.workers.metrics_worker",
        "app.workers.feedback_worker",
        "app.workers.publication_plan_worker",
    ],
)
celery_app.conf.update(
    task_track_started=True,
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=86400,
)
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
    "sync-recent-publication-metrics": {
        "task": "sync_recent_publication_metrics",
        "schedule": settings.metrics_sync_interval_seconds,
    },
}
