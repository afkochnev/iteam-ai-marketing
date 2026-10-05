import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.workers.celery_app import (
    AI_BEAT_SCHEDULE,
    AI_LIVE_TEST_QUEUE,
    ALL_QUEUES,
    PUBLICATION_BEAT_SCHEDULE,
    TASK_ROUTES,
    UNROUTED_QUEUE,
    beat_schedule_for_role,
    celery_app,
)


def routed_queue(task_name: str) -> str:
    route = celery_app.amqp.router.route({}, task_name, args=(), kwargs={})
    queue = route["queue"]
    return str(getattr(queue, "name", queue))


@pytest.mark.parametrize(
    ("task_name", "queue"),
    [
        ("execute_agent_run", "ai"),
        ("index_knowledge_item", "ai"),
        ("generate_feedback_analysis", "ai"),
        ("generate_publication_plan", "ai"),
        ("dispatch_ready_tasks", "ai"),
        ("recover_stuck_ai_tasks", "ai"),
        ("publish_telegram_publication", "publication"),
        ("publish_vk_publication", "publication"),
        ("dispatch_due_publications", "publication_control"),
        ("recover_stuck_publications", "publication_control"),
        ("sync_publication_metrics", "metrics"),
        ("sync_recent_publication_metrics", "metrics"),
    ],
)
def test_business_tasks_have_explicit_routes(task_name: str, queue: str) -> None:
    assert TASK_ROUTES[task_name] == {"queue": queue}
    assert routed_queue(task_name) == queue


def test_unknown_task_fails_safe_to_unrouted() -> None:
    assert routed_queue("unregistered.task") == UNROUTED_QUEUE
    assert celery_app.conf.task_create_missing_queues is False
    assert UNROUTED_QUEUE in ALL_QUEUES


def test_test_context_uses_memory_celery_transport() -> None:
    assert celery_app.conf.broker_url == "memory://"
    assert celery_app.conf.result_backend == "cache+memory://"


def test_beat_schedules_are_disjoint_by_role() -> None:
    ai_tasks = {entry["task"] for entry in beat_schedule_for_role("ai").values()}
    publication_tasks = {entry["task"] for entry in beat_schedule_for_role("publication").values()}
    assert ai_tasks == {"dispatch_ready_tasks", "recover_stuck_ai_tasks"}
    assert publication_tasks == {
        "dispatch_due_publications",
        "recover_stuck_publications",
        "sync_recent_publication_metrics",
    }
    assert ai_tasks.isdisjoint(publication_tasks)
    assert AI_BEAT_SCHEDULE == beat_schedule_for_role("ai")
    assert PUBLICATION_BEAT_SCHEDULE == beat_schedule_for_role("publication")
    assert beat_schedule_for_role("disabled") == {}
    with pytest.raises(ValueError, match="CELERY_BEAT_ROLE"):
        beat_schedule_for_role("combined")


def test_live_test_queue_is_not_consumed_by_normal_ai_worker() -> None:
    compose_file = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    compose = compose_file.read_text(encoding="utf-8")
    assert "worker --queues=ai --loglevel=INFO" in compose
    assert "worker --queues=publication --loglevel=INFO" in compose
    assert "worker --queues=publication_control --loglevel=INFO" in compose
    assert "worker --queues=metrics --loglevel=INFO" in compose
    ai_worker_queues = frozenset({"ai"})
    publication_worker_queues = frozenset({"publication"})
    assert AI_LIVE_TEST_QUEUE not in ai_worker_queues
    assert not ai_worker_queues.intersection(
        {"publication", "publication_control", "metrics", "unrouted", AI_LIVE_TEST_QUEUE}
    )
    assert not publication_worker_queues.intersection({"ai", AI_LIVE_TEST_QUEUE})


def test_ai_worker_fails_fast_if_publication_capability_is_present() -> None:
    safe = Settings(
        _env_file=None,
        celery_role="ai_worker",
        telegram_publishing_enabled=False,
        telegram_bot_token=None,
        telegram_target_chat_id=None,
        vk_publishing_enabled=False,
        vk_access_token=None,
        vk_owner_id=None,
    )
    safe.validate_ai_worker_capabilities()

    with pytest.raises(ValueError, match="publication providers disabled"):
        Settings(
            _env_file=None,
            celery_role="ai_worker",
            telegram_publishing_enabled=True,
            telegram_bot_token=None,
            telegram_target_chat_id=None,
            vk_publishing_enabled=False,
            vk_access_token=None,
            vk_owner_id=None,
        ).validate_ai_worker_capabilities()

    with pytest.raises(ValueError, match="credentials absent"):
        Settings(
            _env_file=None,
            celery_role="ai_worker",
            telegram_publishing_enabled=False,
            telegram_bot_token=None,
            telegram_target_chat_id=None,
            vk_publishing_enabled=False,
            vk_access_token="configured-token",
            vk_owner_id=None,
        ).validate_ai_worker_capabilities()


def test_application_enqueue_sites_do_not_use_default_delay() -> None:
    app_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in app_root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(isinstance(node, ast.Attribute) and node.attr == "delay" for node in ast.walk(tree)):
            offenders.append(str(path.relative_to(app_root)))
    assert offenders == []
