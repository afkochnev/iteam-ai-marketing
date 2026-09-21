from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "iteam_ai_marketing",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.agent_worker"],
)
celery_app.conf.update(task_track_started=True, timezone="UTC", enable_utc=True)
