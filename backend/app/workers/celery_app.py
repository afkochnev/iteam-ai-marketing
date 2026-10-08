from celery import Celery
from kombu import Queue

from app.core.config import settings
from app.workers.scheduler_config import build_beat_schedule

settings.validate_worker_capabilities()
settings.validate_scheduler_capabilities()
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
    task_default_queue="unrouted",
    task_default_exchange="unrouted",
    task_default_routing_key="unrouted",
    task_queues=tuple(
        Queue(name, routing_key=name)
        for name in (
            "ai",
            "ai_control",
            "ai_live_test",
            "publication",
            "publication_control",
            "metrics",
            "unrouted",
        )
    ),
    task_routes={
        "execute_agent_run": {"queue": "ai"},
        "index_knowledge_item": {"queue": "ai"},
        "dispatch_ready_tasks": {"queue": "ai_control"},
        "recover_stuck_ai_tasks": {"queue": "ai_control"},
        "generate_feedback_analysis": {"queue": "ai"},
        "generate_publication_plan": {"queue": "ai"},
        "publish_telegram_publication": {"queue": "publication"},
        "publish_vk_publication": {"queue": "publication"},
        "dispatch_due_publications": {"queue": "publication_control"},
        "recover_stuck_publications": {"queue": "publication_control"},
        "sync_publication_metrics": {"queue": "metrics"},
        "advance_marketing_experiments": {"queue": "metrics"},
        "sync_recent_publication_metrics": {"queue": "metrics"},
    },
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
celery_app.conf.beat_schedule = build_beat_schedule(settings.scheduler_role, settings)
