from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.integrations.metrics import MetricsProviderError, VKMetricsProvider
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource
from app.schemas.publication import PublicationCreate
from app.services.metrics_service import MetricsService
from app.services.publication_service import PublicationService
from app.tests.test_publications import _approved_post


async def _published_publication(session: AsyncSession) -> tuple[Publication, object]:
    user, _campaign, post, version = await _approved_post(session)
    publication = await PublicationService(session).create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    row = await session.get(Publication, publication.id)
    assert row is not None
    row.status = PublicationStatus.PUBLISHED
    row.external_id = "964"
    row.published_at = datetime.now(UTC)
    await session.commit()
    return row, user


@pytest.mark.integration
async def test_manual_metrics_snapshot_preserves_null_and_deduplicates(
    db_session: AsyncSession,
) -> None:
    publication, user = await _published_publication(db_session)
    service = MetricsService(db_session)
    observed = datetime.now(UTC)
    values = {
        "views": 0,
        "impressions": None,
        "reactions": 2,
        "likes": None,
        "comments": 1,
        "shares": None,
        "clicks": None,
        "subscribers": None,
    }
    first = await service.record_manual(publication.id, user, observed_at=observed, values=values)
    second = await service.record_manual(
        publication.id,
        user,
        observed_at=observed + timedelta(minutes=1),
        values=values,
    )
    assert first.id == second.id
    assert first.source is MetricsSource.MANUAL
    assert first.views == 0
    assert first.impressions is None


@pytest.mark.integration
async def test_metrics_reject_non_published_and_naive_time(db_session: AsyncSession) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    publication = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id,
            content_version_id=version.id,
            channel=post.channel,
        ),
        user,
    )
    service = MetricsService(db_session)
    with pytest.raises(AppError) as not_published:
        await service.record_manual(
            publication.id,
            user,
            observed_at=datetime.now(UTC),
            values={"views": 1},
        )
    assert not_published.value.code == "METRICS_PUBLICATION_NOT_PUBLISHED"
    publication_row = await db_session.get(Publication, publication.id)
    assert publication_row is not None
    publication_row.status = PublicationStatus.PUBLISHED
    publication_row.external_id = "964"
    await db_session.commit()
    with pytest.raises(AppError) as naive:
        await service.record_manual(
            publication.id,
            user,
            observed_at=datetime.now(),
            values={"views": 1},
        )
    assert naive.value.code == "METRICS_TIMEZONE_REQUIRED"


@pytest.mark.integration
async def test_telegram_metrics_is_explicitly_unsupported(db_session: AsyncSession) -> None:
    publication, _user = await _published_publication(db_session)
    with pytest.raises(AppError) as error:
        await MetricsService(db_session).sync(publication.id)
    assert error.value.code == "TELEGRAM_METRICS_UNSUPPORTED"
    row = await db_session.get(Publication, publication.id)
    assert row is not None and row.status is PublicationStatus.PUBLISHED


@pytest.mark.integration
async def test_changed_observation_appends_and_negative_values_rejected(
    db_session: AsyncSession,
) -> None:
    publication, user = await _published_publication(db_session)
    service = MetricsService(db_session)
    first = await service.record_manual(
        publication.id,
        user,
        observed_at=datetime.now(UTC),
        values={"views": 10},
    )
    second = await service.record_manual(
        publication.id,
        user,
        observed_at=datetime.now(UTC) + timedelta(minutes=1),
        values={"views": 11},
    )
    assert first.id != second.id
    with pytest.raises(AppError) as error:
        await service.record_manual(
            publication.id,
            user,
            observed_at=datetime.now(UTC),
            values={"views": -1},
        )
    assert error.value.code == "METRICS_INVALID_VALUE"


@pytest.mark.integration
@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.DRAFT,
        PublicationStatus.WAITING_APPROVAL,
        PublicationStatus.APPROVED,
        PublicationStatus.SCHEDULED,
        PublicationStatus.PUBLISHING,
        PublicationStatus.FAILED,
        PublicationStatus.CANCELLED,
    ],
)
async def test_only_published_publications_are_eligible(
    db_session: AsyncSession, status: PublicationStatus
) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    publication = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id,
            content_version_id=version.id,
            channel=post.channel,
        ),
        user,
    )
    publication.status = status
    publication.external_id = "964"
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await MetricsService(db_session).record_manual(
            publication.id,
            user,
            observed_at=datetime.now(UTC),
            values={"views": 1},
        )
    assert error.value.code == "METRICS_PUBLICATION_NOT_PUBLISHED"


@pytest.mark.integration
async def test_vk_metrics_unsupported_preserves_publication(db_session: AsyncSession) -> None:
    publication, _user = await _published_publication(db_session)
    publication.channel = ContentChannel.VK
    before = (publication.status, publication.external_id, publication.published_at)
    with pytest.raises(AppError) as error:
        await MetricsService(db_session).sync(publication.id)
    await db_session.refresh(publication)
    assert error.value.code == "VK_METRICS_UNSUPPORTED"
    assert (publication.status, publication.external_id, publication.published_at) == before


@pytest.mark.integration
async def test_campaign_performance_uses_latest_snapshot_and_coverage(
    db_session: AsyncSession,
) -> None:
    publication, user = await _published_publication(db_session)
    service = MetricsService(db_session)
    old = await service.record_manual(
        publication.id,
        user,
        observed_at=datetime.now(UTC) - timedelta(days=1),
        values={"views": 4, "likes": 1},
    )
    latest = await service.record_manual(
        publication.id,
        user,
        observed_at=datetime.now(UTC),
        values={"views": 0, "likes": None},
    )
    result = await service.campaign_performance(
        publication.campaign_id,
        datetime.now(UTC) - timedelta(days=2),
        datetime.now(UTC) + timedelta(days=1),
    )
    assert old.id != latest.id
    assert result["total_published"] == 1
    assert result["with_metrics"] == 1
    assert result["totals"]["views"] == 0
    assert result["metric_coverage"]["views"] == 1
    assert result["totals"]["likes"] == 0


@pytest.mark.asyncio
async def test_vk_provider_is_explicitly_unsupported() -> None:
    with pytest.raises(MetricsProviderError) as error:
        await VKMetricsProvider().get_metrics(external_id="964")
    assert error.value.code == "VK_METRICS_UNSUPPORTED"
