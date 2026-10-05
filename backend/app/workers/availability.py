import asyncio

from app.workers.celery_app import celery_app


async def queue_has_consumer(queue: str) -> bool | None:
    """Use Celery's existing control API; failed inspection is unknown availability."""
    try:
        active_queues = await asyncio.to_thread(
            celery_app.control.inspect(timeout=0.5).active_queues
        )
    except Exception:
        return None
    return any(
        entry.get("name") == queue for queues in (active_queues or {}).values() for entry in queues
    )
