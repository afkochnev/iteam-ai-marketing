import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select, update

from app.api.publications import publication_metrics
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.integrations.metrics import ProviderMetricsResult, VKMetricsProvider
from app.models.activity import ActivityLog
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.services.metrics_service import MetricsService
from app.tests.test_publication_duplicate_delivery import _issue_claim
from app.workers import vk_worker

pytestmark = pytest.mark.integration


@pytest.fixture
async def published(db_session, monkeypatch):
    with monkeypatch.context() as worker:
        worker.setattr(settings, "vk_owner_id", -987)
        claim = await _issue_claim(db_session, worker, ContentChannel.VK)
        worker.setattr(settings, "worker_role", "publication")
        worker.setattr(settings, "vk_access_token", "fixture-publish-token")
        send = AsyncMock(return_value=httpx.Response(200, json={"response": {"post_id": 314}}))
        worker.setattr(httpx.AsyncClient, "post", send)
        await vk_worker._run(claim.id, claim.token)
        row = await db_session.get(Publication, claim.id, populate_existing=True)
        assert row is not None
        assert row.status is PublicationStatus.PUBLISHED, row.failure_code
        send.assert_awaited_once()
        assert send.await_args.kwargs["data"]["owner_id"] == -987
    monkeypatch.setattr(settings, "worker_role", "metrics")
    monkeypatch.setattr(settings, "vk_metrics_enabled", True)
    monkeypatch.setattr(settings, "vk_metrics_owner_id", -987)
    monkeypatch.setattr(settings, "vk_metrics_access_token", "fixture-read-token")
    return claim


def _response(**fields):
    post = {"owner_id": -987, "id": 314, **fields}
    return httpx.Response(200, json={"response": {"items": [post]}})


@pytest.mark.parametrize("seed_zero", [False, True], ids=["identical", "changed-then-restored"])
async def test_overlapping_reads_preserve_observation_order(
    published, db_session, monkeypatch, seed_zero
):
    empty = await publication_metrics(published.id, published.user, db_session)
    assert empty.external_placement_id is not None
    assert empty.latest is None and empty.history == []

    counts = [0, 1, 0] if seed_zero else [0, 0]
    http_read = AsyncMock(side_effect=[_response(likes={"count": value}) for value in counts])
    monkeypatch.setattr(httpx.AsyncClient, "post", http_read)
    if seed_zero:
        await MetricsService(db_session).sync(published.id)

    first_sampled = asyncio.Event()
    release_first = asyncio.Event()
    real_read = VKMetricsProvider.get_metrics

    async def pause_first(reader, *, external_id):
        sample = await real_read(reader, external_id=external_id)
        if not first_sampled.is_set():
            first_sampled.set()
            await release_first.wait()
        return sample

    monkeypatch.setattr(VKMetricsProvider, "get_metrics", pause_first)
    async with (
        async_session_factory() as first_session,
        async_session_factory() as second_session,
        asyncio.timeout(15),
        asyncio.TaskGroup() as tasks,
    ):
        second_pid = await second_session.scalar(select(func.pg_backend_pid()))
        first = tasks.create_task(MetricsService(first_session).sync(published.id))
        await first_sampled.wait()
        second = tasks.create_task(MetricsService(second_session).sync(published.id))
        try:
            # Wait until PostgreSQL observes the second reader behind the first.
            # A reader that sampled and finished early must also fail this check.
            while not await db_session.scalar(
                select(func.cardinality(func.pg_blocking_pids(second_pid)))
            ):
                assert not second.done()
                await asyncio.sleep(0.01)
        finally:
            release_first.set()

    observed = await publication_metrics(published.id, published.user, db_session)
    assert observed.latest is not None and observed.latest.id == second.result().id
    assert observed.external_placement_id == empty.external_placement_id
    assert all(row.external_placement_id == empty.external_placement_id for row in observed.history)
    targets = [call.kwargs["data"]["posts"] for call in http_read.await_args_list]
    assert targets == ["-987_314"] * len(counts)
    assert observed.latest.views is None
    if seed_zero:
        assert first.result().id != second.result().id
        assert [row.likes for row in observed.history] == [0, 1, 0]
    else:
        assert first.result().id == second.result().id
        assert [row.likes for row in observed.history] == [0]


async def test_cancellation_releases_metrics_lock_with_session_kept_open(
    published, db_session, monkeypatch
):
    entered = asyncio.Event()

    async def never_returns(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async with async_session_factory() as cancelled_session, asyncio.timeout(10):
        with monkeypatch.context() as blocked:
            blocked.setattr(VKMetricsProvider, "get_metrics", never_returns)
            task = asyncio.create_task(MetricsService(cancelled_session).sync(published.id))
            try:
                await entered.wait()
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        monkeypatch.setattr(
            httpx.AsyncClient, "post", AsyncMock(return_value=_response(likes={"count": 0}))
        )
        assert (await MetricsService(db_session).sync(published.id)).likes == 0
        assert not cancelled_session.in_transaction()


async def test_original_unlinked_observations_and_manual_remain_independent(
    published, db_session, monkeypatch
):
    service = MetricsService(db_session)
    legacy_read = ProviderMetricsResult(datetime.now(UTC), views=0, provider="vk_wall")
    with monkeypatch.context() as legacy:
        legacy.setattr(VKMetricsProvider, "get_metrics", AsyncMock(return_value=legacy_read))
        original = await service.sync(published.id)
    assert original.external_placement_id is None
    manual = await service.record_manual(
        published.id, published.user, observed_at=datetime.now(UTC), values={"views": 0}
    )
    assert manual.source is MetricsSource.MANUAL and manual.external_placement_id is None

    monkeypatch.setattr(
        httpx.AsyncClient, "post", AsyncMock(return_value=_response(views={"count": 0}))
    )
    verified = await service.sync(published.id)
    assert verified.external_placement_id is not None
    assert verified.id not in {original.id, manual.id}
    assert (await service.sync(published.id)).id == verified.id
    await db_session.refresh(original)
    assert original.external_placement_id is None
    report = await publication_metrics(published.id, published.user, db_session)
    assert len(report.history) == 3


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({}, "METRICS_NO_DATA"),
        ({"id": 314.0, "views": {"count": 0}}, "VK_METRICS_POST_MISMATCH"),
        ({"owner_id": -987.0, "views": {"count": 0}}, "VK_METRICS_POST_MISMATCH"),
        ({"views": {"count": False}}, "METRICS_RESPONSE_INVALID"),
        ({"views": {"count": -1}}, "METRICS_RESPONSE_INVALID"),
    ],
)
async def test_unusable_receipt_leaves_placement_without_observations(
    published, db_session, monkeypatch, fields, code
):
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(return_value=_response(**fields)))
    with pytest.raises(AppError) as error:
        await MetricsService(db_session).sync(published.id)
    assert error.value.code == code
    report = await publication_metrics(published.id, published.user, db_session)
    assert report.external_placement_id is not None and not report.history


@pytest.mark.parametrize("external_id", ["314", "0314"])
async def test_foreign_publication_cannot_borrow_placement(
    published, db_session, monkeypatch, external_id
):
    own = await db_session.get(Publication, published.id, populate_existing=True)
    other = Publication(
        campaign_id=own.campaign_id,
        content_item_id=own.content_item_id,
        content_version_id=own.content_version_id,
        channel=own.channel,
        status=PublicationStatus.PUBLISHED,
        external_id=external_id,
        provider_target_id=own.provider_target_id,
        published_at=own.published_at,
    )
    db_session.add(other)
    await db_session.commit()
    monkeypatch.setattr(
        httpx.AsyncClient, "post", AsyncMock(return_value=_response(views={"count": 0}))
    )
    other_id = other.id
    service = MetricsService(db_session)
    original_id = (await service.sync(published.id)).id
    with pytest.raises(AppError) as error:
        await service.sync(other_id)
    assert error.value.code == "METRICS_PLACEMENT_CONFLICT"

    # A rejected sync must release its transaction lock even when the caller keeps the session.
    async with async_session_factory() as checker:
        available = await checker.scalar(
            select(Publication.id).where(Publication.id == other_id).with_for_update(nowait=True)
        )
        assert available == other_id
    report = await publication_metrics(other_id, published.user, db_session)
    assert report.external_placement_id is None and report.history == []
    original = await publication_metrics(published.id, published.user, db_session)
    assert [row.id for row in original.history] == [original_id]
    synced = (
        await db_session.scalars(
            select(ActivityLog).where(ActivityLog.event_type == "PUBLICATION_METRICS_SYNCED")
        )
    ).all()
    assert [event.metadata_["publication_id"] for event in synced] == [str(published.id)]


async def test_publication_correction_during_remote_read_discards_sample(
    published, db_session, monkeypatch
):
    async def read(_client, _url, *, data):
        assert data["posts"] == "-987_314"
        async with async_session_factory() as editor:
            await editor.execute(
                update(Publication).where(Publication.id == published.id).values(external_id="315")
            )
            await editor.commit()
        return _response(views={"count": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", read)
    with pytest.raises(AppError) as error:
        await asyncio.wait_for(MetricsService(db_session).sync(published.id), timeout=15)
    assert error.value.code == "METRICS_PUBLICATION_CHANGED"
    report = await publication_metrics(published.id, published.user, db_session)
    assert report.external_placement_id is None and not report.history


async def test_wrong_reader_target_cannot_supply_placement_evidence(
    published, db_session, monkeypatch
):
    # Change the read target after the service checks its configuration, before the adapter reads.
    read = VKMetricsProvider.get_metrics

    async def drift(reader, *, external_id):
        monkeypatch.setattr(settings, "vk_metrics_owner_id", -999)
        return await read(reader, external_id=external_id)

    async def http_read(_client, _url, *, data):
        assert data["posts"] == "-999_314"
        return _response(owner_id=-999, views={"count": 0})

    monkeypatch.setattr(VKMetricsProvider, "get_metrics", drift)
    monkeypatch.setattr(httpx.AsyncClient, "post", http_read)
    with pytest.raises(AppError) as error:
        await MetricsService(db_session).sync(published.id)
    assert error.value.code == "METRICS_PROVIDER_TARGET_MISMATCH"
    assert await db_session.scalar(select(PublicationMetricsSnapshot.id)) is None
