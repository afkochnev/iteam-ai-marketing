import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, update

from app.api.publications import publication_metrics
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.integrations.metrics import ProviderMetricsResult, VKMetricsProvider
from app.integrations.telegram import ProviderPublicationResult, VKProvider
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.services.metrics_service import MetricsService
from app.tests.test_publication_duplicate_delivery import _issue_claim
from app.workers import vk_worker

pytestmark = pytest.mark.integration


@pytest.fixture
async def published(db_session, monkeypatch):
    monkeypatch.setattr(settings, "vk_owner_id", -987)
    claim = await _issue_claim(db_session, monkeypatch, ContentChannel.VK)
    send = AsyncMock(return_value=ProviderPublicationResult("314", None, datetime.now(UTC), "-987"))
    monkeypatch.setattr(VKProvider, "publish", send)
    await vk_worker._run(claim.id, claim.token)
    send.assert_awaited_once()
    monkeypatch.setattr(settings, "worker_role", "metrics")
    monkeypatch.setattr(settings, "vk_metrics_enabled", True)
    monkeypatch.setattr(settings, "vk_metrics_owner_id", -987)
    monkeypatch.setattr(settings, "vk_metrics_access_token", "fixture-read-token")
    return claim


def _response(**fields):
    post = {"owner_id": -987, "id": 314, **fields}
    return httpx.Response(200, json={"response": {"items": [post]}})


async def test_published_placement_then_overlapping_first_observed_zero(
    published, db_session, monkeypatch
):
    empty = await publication_metrics(published.id, published.user, db_session)
    assert empty.external_placement_id is not None
    assert empty.latest is None and empty.history == []

    both_reading = asyncio.Event()
    calls = []

    async def read(_client, _url, *, data):
        calls.append(data["posts"])
        if len(calls) == 2:
            both_reading.set()
        await asyncio.wait_for(both_reading.wait(), timeout=10)
        return _response(views={"count": 0})

    monkeypatch.setattr(httpx.AsyncClient, "post", read)

    async def sync():
        async with async_session_factory() as session:
            return (await MetricsService(session).sync(published.id)).id

    first, duplicate = await asyncio.wait_for(asyncio.gather(sync(), sync()), timeout=15)
    assert first == duplicate
    assert calls == ["-987_314", "-987_314"]
    observed = await publication_metrics(published.id, published.user, db_session)
    assert observed.latest is not None and observed.history == [observed.latest]
    assert observed.latest.id == first
    assert observed.latest.external_placement_id == observed.external_placement_id
    assert observed.latest.external_placement_id == empty.external_placement_id
    assert observed.latest.views == 0 and observed.latest.likes is None


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


async def test_foreign_publication_cannot_borrow_placement(published, db_session, monkeypatch):
    own = await db_session.get(Publication, published.id, populate_existing=True)
    other = Publication(
        campaign_id=own.campaign_id,
        content_item_id=own.content_item_id,
        content_version_id=own.content_version_id,
        channel=own.channel,
        status=PublicationStatus.PUBLISHED,
        external_id=own.external_id,
        provider_target_id=own.provider_target_id,
        published_at=own.published_at,
    )
    db_session.add(other)
    await db_session.commit()
    monkeypatch.setattr(
        httpx.AsyncClient, "post", AsyncMock(return_value=_response(views={"count": 0}))
    )
    snapshot = await MetricsService(db_session).sync(other.id)
    assert snapshot.external_placement_id is None
    report = await publication_metrics(other.id, published.user, db_session)
    assert report.external_placement_id is None
    assert [row.id for row in report.history] == [snapshot.id]


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
