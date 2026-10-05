from celery import Celery
from kombu import Exchange, Queue

from app.core.config import settings

settings.validate_redis_isolation()
settings.validate_ai_worker_capabilities()

AI_QUEUE = "ai"
AI_LIVE_TEST_QUEUE = "ai_live_test"
PUBLICATION_QUEUE = "publication"
PUBLICATION_CONTROL_QUEUE = "publication_control"
METRICS_QUEUE = "metrics"
UNROUTED_QUEUE = "unrouted"

TASK_ROUTES: dict[str, dict[str, str]] = {
    "execute_agent_run": {"queue": AI_QUEUE},
    "index_knowledge_item": {"queue": AI_QUEUE},
    "generate_feedback_analysis": {"queue": AI_QUEUE},
    "generate_publication_plan": {"queue": AI_QUEUE},
    "dispatch_ready_tasks": {"queue": AI_QUEUE},
    "recover_stuck_ai_tasks": {"queue": AI_QUEUE},
    "publish_telegram_publication": {"queue": PUBLICATION_QUEUE},
    "publish_vk_publication": {"queue": PUBLICATION_QUEUE},
    "dispatch_due_publications": {"queue": PUBLICATION_CONTROL_QUEUE},
    "recover_stuck_publications": {"queue": PUBLICATION_CONTROL_QUEUE},
    "sync_publication_metrics": {"queue": METRICS_QUEUE},
    "sync_recent_publication_metrics": {"queue": METRICS_QUEUE},
}

ALL_QUEUES = (
    AI_QUEUE,
    AI_LIVE_TEST_QUEUE,
    PUBLICATION_QUEUE,
    PUBLICATION_CONTROL_QUEUE,
    METRICS_QUEUE,
    UNROUTED_QUEUE,
)

AI_BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "dispatch-ready-ai-tasks": {
        "task": "dispatch_ready_tasks",
        "schedule": settings.task_dispatch_interval_seconds,
    },
    "recover-stuck-ai-tasks": {
        "task": "recover_stuck_ai_tasks",
        "schedule": settings.task_dispatch_interval_seconds,
    },
}

PUBLICATION_BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    "dispatch-due-publications": {
        "task": "dispatch_due_publications",
        "schedule": settings.task_dispatch_interval_seconds,
    },
    "recover-stuck-publications": {
        "task": "recover_stuck_publications",
        "schedule": settings.task_dispatch_interval_seconds,
    },
    "sync-recent-publication-metrics": {
        "task": "sync_recent_publication_metrics",
        "schedule": settings.metrics_sync_interval_seconds,
    },
}


def beat_schedule_for_role(role: str) -> dict[str, dict[str, object]]:
    """Return a disjoint schedule for one scheduler process."""
    if role == "ai":
        return dict(AI_BEAT_SCHEDULE)
    if role == "publication":
        return dict(PUBLICATION_BEAT_SCHEDULE)
    if role == "disabled":
        return {}
    raise ValueError(f"Unsupported CELERY_BEAT_ROLE: {role}")


celery_app = Celery(
    "iteam_ai_marketing",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend_url,
    include=[
        "app.workers.agent_worker",
        "app.workers.knowledge_worker",
        "app.workers.dispatcher_worker",
        "app.workers.recovery_worker",
        "app.workers.publication_recovery_worker",
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
    task_default_queue=UNROUTED_QUEUE,
    task_default_exchange=UNROUTED_QUEUE,
    task_default_routing_key=UNROUTED_QUEUE,
    task_create_missing_queues=False,
    task_queues=tuple(
        Queue(name, Exchange(name, type="direct"), routing_key=name) for name in ALL_QUEUES
    ),
    task_routes=TASK_ROUTES,
    beat_schedule=beat_schedule_for_role(settings.celery_beat_role),
)
