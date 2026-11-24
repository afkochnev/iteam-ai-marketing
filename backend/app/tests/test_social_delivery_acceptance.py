"""Legacy social publication through the dispatcher, Redis and a real Telegram task."""

from datetime import UTC, datetime, timedelta
from queue import Queue
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from celery.contrib.testing.app import TestApp, setup_default_app
from celery.contrib.testing.worker import start_worker
from celery.signals import task_postrun
from redis import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.test_redis import ensure_safe_test_redis_url
from app.integrations.telegram import ProviderPublicationResult, TelegramProvider
from app.models.activity import ActivityLog
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.services import publication_service
from app.services.distribution_workspace_service import DistributionWorkspaceService
from app.services.publication_service import PublicationService
from app.tests.test_plan_publication_scheduling import plan_post
from app.tests.test_publication_replan_versions import _approve_newer_revision
from app.workers import dispatcher_worker


@pytest.fixture
def publication_broker(monkeypatch):
    url = ensure_safe_test_redis_url(settings.redis_url)
    prefix = f"social-delivery-test:{uuid4().hex}:"
    # Celery's environment overrides take precedence over application configuration.
    for name in ("CELERY_BROKER_URL", "CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL"):
        monkeypatch.setenv(name, url)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "cache+memory://")
    monkeypatch.delenv("TEST_BROKER", raising=False)
    app = TestApp(
        broker=url,
        config={
            "broker_transport_options": {"global_keyprefix": prefix},
            "task_acks_late": True,
            "task_ignore_result": True,
            "worker_enable_remote_control": False,
        },
    )
    with setup_default_app(app), Redis.from_url(url) as redis:
        try:
            yield app, redis, prefix + "publication"
        finally:
            keys = list(redis.scan_iter(match=prefix + "*"))
            if keys:
                redis.delete(*keys)


@pytest.mark.integration
async def test_replanned_telegram_delivery_survives_duplicate_message(
    db_session: AsyncSession, publication_broker, monkeypatch
) -> None:
    app, redis, queue_key = publication_broker
    user, campaign, post, first, _slot = await plan_post(db_session, ContentChannel.TELEGRAM)
    service = PublicationService(db_session)
    original = await service.schedule_content(post.id, user)
    second = await _approve_newer_revision(db_session, post, user)
    assert post.current_version_id == second.id and first.content != second.content

    # The operator chose this slot one minute ago. The real dispatcher sees it due now.
    due = datetime.now(UTC)
    with monkeypatch.context() as clock:
        clock.setattr(publication_service, "utc_now", lambda: due - timedelta(minutes=1))
        replacement = await service.replan(original.id, due, user)
    assert replacement.content_version_id == first.id

    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    monkeypatch.setattr(settings, "telegram_bot_token", "synthetic-token")
    monkeypatch.setattr(settings, "telegram_target_chat_id", "synthetic-chat")
    receipt = ProviderPublicationResult("197", None, due)
    send = AsyncMock(return_value=receipt)
    monkeypatch.setattr(TelegramProvider, "publish", send)

    # Bind the production task to the test broker. Keep the dispatcher's real queue route.
    task = app.tasks["publish_telegram_publication"]
    monkeypatch.setattr(dispatcher_worker, "publish_telegram_publication", task)
    await dispatcher_worker._dispatch_publications()
    assert redis.llen(queue_key) == 1
    envelope = redis.lindex(queue_key, 0)
    row = await db_session.get(Publication, replacement.id, populate_existing=True)
    assert row is not None and row.status is PublicationStatus.PUBLISHING
    assert row.execution_token
    expected_args = (str(row.id), row.execution_token)

    completed = Queue()

    def record_completion(task_id, args, state, **kwargs):
        completed.put((task_id, tuple(args), state))

    task_postrun.connect(record_completion, sender=task, weak=False)
    try:
        with start_worker(app, queues=("publication",), pool="solo", perform_ping_check=False):
            task_id, args, state = completed.get(timeout=20)
            assert args == expected_args and state == "SUCCESS"
            send.assert_awaited_once_with(text=first.content, chat_id="synthetic-chat")
            await db_session.refresh(row)
            assert row.status is PublicationStatus.PUBLISHED
            assert row.external_id == receipt.external_id
            # Replay the actual serialized message including the original Celery ID and token.
            redis.lpush(queue_key, envelope)
            assert completed.get(timeout=20) == (task_id, expected_args, "SUCCESS")
    finally:
        task_postrun.disconnect(record_completion, sender=task)

    send.assert_awaited_once_with(text=first.content, chat_id="synthetic-chat")
    await db_session.refresh(row)
    assert row.status is PublicationStatus.PUBLISHED
    assert row.external_id == receipt.external_id and row.published_at == receipt.published_at
    old = await db_session.get(Publication, original.id, populate_existing=True)
    assert old is not None and old.status is PublicationStatus.CANCELLED
    assert old.external_id is None
    events = (
        await db_session.scalars(
            select(ActivityLog).where(
                ActivityLog.content_item_id == post.id,
                ActivityLog.event_type == "PUBLICATION_PUBLISHED",
            )
        )
    ).all()
    assert len(events) == 1
    assert events[0].metadata_ == {
        "publication_id": str(row.id),
        "external_id": receipt.external_id,
    }
    workspace = await DistributionWorkspaceService(db_session).get(
        user, campaign_id=campaign.id, publication_id=row.id, limit=1
    )
    (card,) = workspace.items
    assert card.publication_id == row.id and card.content_version_id == first.id
    assert card.status is PublicationStatus.PUBLISHED and "published" in card.sections
