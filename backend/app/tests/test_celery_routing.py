from pathlib import Path
from shlex import split
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from celery import Celery

from app.api import publications as publication_api
from app.core.config import settings
from app.core.errors import AppError
from app.models.agent_run import AgentRunStatus
from app.models.content import ContentChannel
from app.models.publication import PublicationStatus
from app.services.agent_run_service import AgentRunService
from app.services.knowledge_service import KnowledgeService
from app.workers import agent_worker, dispatcher_worker, metrics_worker, telegram_worker, vk_worker
from app.workers.celery_app import celery_app
from app.workers.scheduler_config import build_beat_schedule


@pytest.fixture
def broker_app():
    # Production routing with an in-process transport; no worker or external connection.
    app = Celery(
        "routing_regression", broker="memory://", backend="cache+memory://", set_as_current=False
    )
    app.conf.update(
        **{
            key: celery_app.conf[key]
            for key in (
                "task_default_queue",
                "task_default_exchange",
                "task_default_routing_key",
                "task_queues",
                "task_routes",
            )
        }
    )

    # Kombu's memory transport is process-global, including messages sent by
    # earlier API tests. Keep these routing assertions independent of test order.
    def clear_queues():
        with app.connection_for_write() as connection:
            for declared_queue in app.conf.task_queues:
                queue = connection.SimpleQueue(declared_queue.name)
                try:
                    queue.clear()
                finally:
                    queue.close()

    clear_queues()
    try:
        yield app
    finally:
        clear_queues()
        app.close()


def assert_message(app, task_name, expected_queue, *, options=None):
    job = app.send_task(task_name, args=["test-id"], **(options or {}))
    with app.connection_for_read() as connection:
        queue = connection.SimpleQueue(expected_queue)
        try:
            message = queue.get(block=False)
            assert message.headers["task"] == task_name
            assert message.headers["id"] == job.id
            assert message.delivery_info["routing_key"] == expected_queue
            message.ack()
        finally:
            queue.close()


@pytest.mark.parametrize(
    ("task_name", "queue"),
    [
        ("execute_agent_run", "ai"),
        ("generate_feedback_analysis", "ai"),
        ("generate_publication_plan", "ai"),
        ("publish_telegram_publication", "publication"),
        ("publish_vk_publication", "publication"),
        ("dispatch_due_publications", "publication_control"),
        ("sync_publication_metrics", "metrics"),
        ("sync_recent_publication_metrics", "metrics"),
        ("index_knowledge_item", "ai"),
        ("dispatch_ready_tasks", "ai_control"),
        ("recover_stuck_ai_tasks", "ai_control"),
        ("recover_stuck_publications", "publication_control"),
        ("recover_stuck_tasks", "unrouted"),
        ("unknown_business_task", "unrouted"),
    ],
)
def test_published_message_routing(broker_app, task_name, queue):
    assert_message(broker_app, task_name, queue)


def test_explicit_isolated_queue_overrides_normal_ai_route(broker_app):
    assert_message(
        broker_app, "execute_agent_run", "ai_live_test", options={"queue": "ai_live_test"}
    )


def test_declared_queues_exclude_legacy_default():
    assert {queue.name for queue in celery_app.conf.task_queues} == {
        "ai",
        "ai_control",
        "ai_live_test",
        "publication",
        "publication_control",
        "metrics",
        "unrouted",
    }


@pytest.mark.parametrize(
    ("role", "entry_name", "expected_queue"),
    [
        ("ai", "dispatch-ready-ai-tasks", "ai_control"),
        ("ai", "recover-stuck-ai-tasks", "ai_control"),
        ("publication", "dispatch-due-publications", "publication_control"),
        ("publication", "recover-stuck-publications", "publication_control"),
        ("publication", "sync-recent-publication-metrics", "metrics"),
    ],
)
def test_beat_uses_explicit_queue(broker_app, role, entry_name, expected_queue):
    entry = build_beat_schedule(role, settings)[entry_name]
    assert entry["options"]["queue"] == expected_queue
    assert_message(broker_app, entry["task"], expected_queue, options=entry["options"])


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("countdown", [0, 45])
async def test_agent_enqueue_and_retry_keep_queue_and_job_id(monkeypatch, isolated, countdown):
    run = SimpleNamespace(id=uuid4(), input_data={"isolated_ai_execution": isolated})
    session = AsyncMock()
    service = AgentRunService(session)
    service.repository = SimpleNamespace(update=AsyncMock())
    service.get_run = AsyncMock(return_value=run)
    enqueue = Mock(return_value=SimpleNamespace(id="queue-job"))
    monkeypatch.setattr(agent_worker.execute_agent_run, "apply_async", enqueue)
    assert await service.enqueue(run, countdown=countdown) is run
    enqueue.assert_called_once_with(
        args=[str(run.id)], queue="ai_live_test" if isolated else "ai", countdown=countdown
    )
    service.repository.update.assert_awaited_once_with(run, {"queue_job_id": "queue-job"})
    session.commit.assert_awaited_once()


async def test_agent_enqueue_failure_is_recorded(monkeypatch):
    run = SimpleNamespace(id=uuid4(), input_data={})
    session = AsyncMock()
    service = AgentRunService(session)
    service.repository = SimpleNamespace(update=AsyncMock())
    monkeypatch.setattr(
        agent_worker.execute_agent_run, "apply_async", Mock(side_effect=RuntimeError("broker down"))
    )
    with pytest.raises(AppError) as error:
        await service.enqueue(run)
    assert error.value.code == "QUEUE_ENQUEUE_FAILED"
    values = service.repository.update.await_args.args[1]
    assert values["status"] == AgentRunStatus.FAILED
    assert values["error_code"] == "QUEUE_ENQUEUE_FAILED"
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("channel", [ContentChannel.TELEGRAM, ContentChannel.VK])
@pytest.mark.parametrize("retry", [False, True])
async def test_publication_api_routes_execution_with_claim_token(monkeypatch, channel, retry):
    publication = SimpleNamespace(
        id=uuid4(),
        channel=channel,
        execution_token="claim-token",
        status=PublicationStatus.FAILED,
        failure_code="PUBLICATION_RECONCILED_NOT_PUBLISHED",
        retry_count=0,
        campaign_id=uuid4(),
        content_item_id=uuid4(),
    )
    session = AsyncMock()
    session.get.return_value = publication
    service = SimpleNamespace(
        claim_for_publish=AsyncMock(return_value=publication),
        _locked=AsyncMock(return_value=publication),
    )
    monkeypatch.setattr(publication_api, "PublicationService", lambda _session: service)
    monkeypatch.setattr(publication_api.ActivityLogService, "record", AsyncMock())
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)

    async def claim(*_args):
        publication.execution_token = "claim-token"
        return publication

    service.claim_for_publish.side_effect = claim
    telegram = Mock()
    vk = Mock()
    monkeypatch.setattr(telegram_worker.publish_telegram_publication, "apply_async", telegram)
    monkeypatch.setattr(vk_worker.publish_vk_publication, "apply_async", vk)
    endpoint = publication_api.retry_publication if retry else publication_api.publish_now
    assert await endpoint(publication.id, SimpleNamespace(id=uuid4()), session) is publication
    selected = vk if channel == ContentChannel.VK else telegram
    other = telegram if channel == ContentChannel.VK else vk
    selected.assert_called_once_with(args=[str(publication.id), "claim-token"], queue="publication")
    other.assert_not_called()


def mock_worker_session(monkeypatch, worker, session):
    engine = SimpleNamespace(dispose=AsyncMock())
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(worker, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "async_sessionmaker", Mock(return_value=lambda: context))
    return engine


@pytest.mark.parametrize("channel", [ContentChannel.TELEGRAM, ContentChannel.VK])
async def test_publication_dispatch_routes_claimed_execution(monkeypatch, channel):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    row = SimpleNamespace(id=uuid4(), channel=channel, execution_token="dispatch-token")
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(all=lambda: [row.id])
    session.get.return_value = row
    engine = mock_worker_session(monkeypatch, dispatcher_worker, session)
    claim = AsyncMock()
    monkeypatch.setattr(dispatcher_worker.PublicationService, "claim_for_publish", claim)
    task = (
        vk_worker.publish_vk_publication
        if channel == ContentChannel.VK
        else telegram_worker.publish_telegram_publication
    )
    enqueue = Mock()
    monkeypatch.setattr(task, "apply_async", enqueue)
    await dispatcher_worker._dispatch_publications()
    claim.assert_awaited_once_with(row.id)
    enqueue.assert_called_once_with(args=[str(row.id), "dispatch-token"], queue="publication")
    session.rollback.assert_not_awaited()
    engine.dispose.assert_awaited_once()


async def test_recent_metrics_fanout_routes_each_publication(monkeypatch):
    rows = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
    session = AsyncMock()
    session.scalars.return_value = SimpleNamespace(all=lambda: rows)
    engine = mock_worker_session(monkeypatch, metrics_worker, session)
    enqueue = Mock()
    monkeypatch.setattr(metrics_worker.sync_publication_metrics, "apply_async", enqueue)
    await metrics_worker._sync_recent()
    assert enqueue.call_count == len(rows)
    for row in rows:
        enqueue.assert_any_call(args=[str(row.id)], queue="metrics")
    engine.dispose.assert_awaited_once()


async def test_knowledge_enqueue_explicitly_uses_ai(monkeypatch):
    from app.workers.knowledge_worker import index_knowledge_item

    item = SimpleNamespace(id=uuid4())
    service = KnowledgeService(AsyncMock())
    service.get_item = AsyncMock(return_value=item)
    enqueue = Mock()
    monkeypatch.setattr(index_knowledge_item, "apply_async", enqueue)
    assert await service.enqueue_indexing(item) is item
    enqueue.assert_called_once_with(args=[str(item.id)], queue="ai")


async def test_metrics_api_routes_sync_to_metrics(monkeypatch):
    publication_id = uuid4()
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(status=PublicationStatus.PUBLISHED)
    monkeypatch.setattr(
        publication_api.MetricsService,
        "publication_metrics",
        AsyncMock(
            return_value={
                "publication_id": publication_id,
                "sync_capable": True,
                "latest": None,
                "history": [],
            }
        ),
    )
    enqueue = Mock()
    monkeypatch.setattr(metrics_worker.sync_publication_metrics, "apply_async", enqueue)
    response = await publication_api.sync_publication_metrics(publication_id, None, session)
    assert response.publication_id == publication_id
    enqueue.assert_called_once_with(args=[str(publication_id)], queue="metrics")


def test_production_workers_exclude_unrouted_and_consume_ai_control():
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    worker_queues = []
    for line in compose_path.read_text().splitlines():
        if not line.strip().startswith("command:"):
            continue
        command = split(line.partition(":")[2])
        if "celery" not in command or "worker" not in command:
            continue
        queues_option = next((arg for arg in command if arg.startswith("--queues=")), None)
        assert queues_option is not None, "Production worker must explicitly select queues"
        queues = set(queues_option.partition("=")[2].split(","))
        assert "unrouted" not in queues, "Fail-safe queue must not be consumed in production"
        worker_queues.append(queues)
    assert worker_queues, "No production Celery worker found"
    assert {"ai", "ai_control", "publication", "publication_control", "metrics"} <= set.union(
        *worker_queues
    )
