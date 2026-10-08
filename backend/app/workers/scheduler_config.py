from typing import Any

from app.core.config import Settings


def build_beat_schedule(role: str | None, config: Settings) -> dict[str, Any]:
    """Select one scheduler's jobs before Beat starts; producers/workers have none."""
    if role is None:
        return {}
    if role == "ai":
        return {
            "dispatch-ready-ai-tasks": {
                "task": "dispatch_ready_tasks",
                "schedule": config.task_dispatch_interval_seconds,
                "options": {"queue": "ai_control"},
            },
            "recover-stuck-ai-tasks": {
                "task": "recover_stuck_ai_tasks",
                "schedule": config.task_dispatch_interval_seconds,
                "options": {"queue": "ai_control"},
            },
        }
    if role == "publication":
        return {
            "dispatch-due-publications": {
                "task": "dispatch_due_publications",
                "schedule": config.task_dispatch_interval_seconds,
                "options": {"queue": "publication_control"},
            },
            "recover-stuck-publications": {
                "task": "recover_stuck_publications",
                "schedule": config.task_dispatch_interval_seconds,
                "options": {"queue": "publication_control"},
            },
            "advance-marketing-experiments": {
                "task": "advance_marketing_experiments",
                "schedule": config.metrics_sync_interval_seconds,
                "options": {"queue": "metrics"},
            },
            "sync-recent-publication-metrics": {
                "task": "sync_recent_publication_metrics",
                "schedule": config.metrics_sync_interval_seconds,
                "options": {"queue": "metrics"},
            },
        }
    raise RuntimeError("SCHEDULER_ROLE is invalid")
