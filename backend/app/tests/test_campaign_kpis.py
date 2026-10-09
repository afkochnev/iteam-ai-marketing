import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_kpi import CampaignKPI, KPIMetric
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.schemas.campaign_kpi import KPICreate, KPIUpdate
from app.services.auth_service import AuthService
from app.services.campaign_kpi_service import CampaignKPIService
from app.services.campaign_performance_service import RAW_FIELDS, observed
from app.services.metrics_service import MetricsService
from app.tests.test_metrics import _published_publication

START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=10)


def config(**changes):
    return KPICreate.model_validate(
        {
            "metric": "VIEWS",
            "target_value": "10",
            "comparison": "GTE",
            "period_start": START,
            "period_end": END,
            **changes,
        }
    )


def values(**changes):
    return {field: changes.get(field) for field in RAW_FIELDS}


@pytest.mark.parametrize(
    "changes",
    [
        {"metric": "ROI"},
        {"comparison": "EQ"},
        {"channel": "FACEBOOK"},
        {"target_value": -1},
        {"target_value": "NaN"},
        {"target_value": "Infinity"},
        {"period_start": END},
        {"period_end": START},
        {"period_start": START.replace(tzinfo=None)},
    ],
)
def test_config_validation(changes):
    with pytest.raises(ValidationError):
        config(**changes)


@pytest.mark.parametrize("metric", list(KPIMetric))
def test_metric_allowlist(metric):
    assert config(metric=metric).metric == metric


@pytest.mark.parametrize(
    "changes",
    [
        {"metric": None},
        {"period_start": None},
        {"target_value": None},
        {"comparison": None},
        {"is_active": None},
    ],
)
def test_patch_rejects_explicit_null_required(changes):
    with pytest.raises(ValidationError):
        KPIUpdate.model_validate(changes)


@pytest.mark.parametrize(
    "metric,raw,expected",
    [
        (KPIMetric.CTR, {"clicks": 5, "impressions": 100}, Decimal("0.05")),
        (KPIMetric.CTR, {"clicks": 0, "impressions": 100}, Decimal(0)),
        (KPIMetric.CTR, {"clicks": 5}, None),
        (KPIMetric.CTR, {"clicks": 5, "impressions": 0}, None),
        (KPIMetric.CTR, {"impressions": 100}, None),
        (
            KPIMetric.ENGAGEMENT_RATE,
            {"reactions": 2, "comments": 3, "shares": 5, "impressions": 100},
            Decimal("0.1"),
        ),
        (
            KPIMetric.ENGAGEMENT_RATE,
            {"reactions": 0, "comments": 0, "shares": 0, "impressions": 100},
            Decimal(0),
        ),
        (KPIMetric.ENGAGEMENT_RATE, {"reactions": 2, "comments": 3, "impressions": 100}, None),
        (KPIMetric.ENGAGEMENT_RATE, {"reactions": 2, "shares": 3, "impressions": 100}, None),
        (KPIMetric.ENGAGEMENT_RATE, {"comments": 2, "shares": 3, "impressions": 100}, None),
        (
            KPIMetric.ENGAGEMENT_RATE,
            {"reactions": 2, "comments": 3, "shares": 5, "impressions": 0},
            None,
        ),
        (KPIMetric.VIEWS, {"views": 0}, Decimal(0)),
        (KPIMetric.VIEWS, {}, None),
    ],
)
def test_formulas_preserve_null(metric, raw, expected):
    assert observed([values(**raw)], metric) == expected


def test_derived_aggregate_does_not_mix_incomplete_publications():
    rows = [
        values(clicks=10, impressions=100),
        values(impressions=900),
        values(clicks=20, impressions=100),
    ]
    assert observed(rows, KPIMetric.CTR) == Decimal("0.15")
    assert observed([values(clicks=1), values(impressions=100)], KPIMetric.CTR) is None


async def setup(session):
    pub, user = await _published_publication(session)
    pub.published_at = START + timedelta(days=1)
    await session.commit()
    return pub, user


async def snapshot(session, pub, **raw):
    row = PublicationMetricsSnapshot(
        publication_id=pub.id,
        channel=pub.channel,
        observed_at=START + timedelta(days=2),
        source=MetricsSource.MANUAL,
        **values(**raw),
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.integration
async def test_crud_limit_deactivation_reactivation_and_patch_validation(db_session):
    pub, _ = await setup(db_session)
    service = CampaignKPIService(db_session)
    rows = [await service.create(pub.campaign_id, config()) for _ in range(5)]
    with pytest.raises(AppError) as err:
        await service.create(pub.campaign_id, config())
    assert err.value.code == "KPI_LIMIT_REACHED"
    await db_session.rollback()
    await db_session.refresh(pub)
    for row in rows:
        await db_session.refresh(row)
    inactive = await service.create(pub.campaign_id, config(is_active=False))
    with pytest.raises(AppError):
        await service.update(inactive.id, KPIUpdate(is_active=True))
    await db_session.rollback()
    await db_session.refresh(pub)
    for row in rows:
        await db_session.refresh(row)
    await db_session.refresh(inactive)
    await service.update(rows[0].id, KPIUpdate(is_active=False))
    await service.update(
        inactive.id, KPIUpdate(is_active=True, target_value=0, channel=ContentChannel.VK)
    )
    with pytest.raises(AppError) as err:
        await service.update(inactive.id, KPIUpdate(period_start=END))
    assert err.value.status_code == 422
    await db_session.rollback()
    await db_session.refresh(pub)
    for row in rows:
        await db_session.refresh(row)
    await db_session.refresh(inactive)
    await service.delete(inactive.id)
    assert len(await service.list_for_campaign(pub.campaign_id)) == 5


@pytest.mark.integration
async def test_concurrent_six_creates_max_five(db_session):
    pub, _ = await setup(db_session)
    cid = pub.campaign_id

    async def create():
        async with async_session_factory() as session:
            try:
                return await CampaignKPIService(session).create(cid, config())
            except AppError as error:
                await session.rollback()
                return error.code

    results = await asyncio.gather(*(create() for _ in range(6)))
    assert results.count("KPI_LIMIT_REACHED") == 1
    assert await db_session.scalar(select(func.count()).select_from(CampaignKPI)) == 5


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["create", "update", "delete", "deactivate"])
async def test_archive_readable_mutations_blocked(db_session, operation):
    pub, _ = await setup(db_session)
    service = CampaignKPIService(db_session)
    kpi = await service.create(pub.campaign_id, config())
    campaign = await db_session.get(Campaign, pub.campaign_id)
    campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    assert len(await service.list_for_campaign(pub.campaign_id)) == 1
    with pytest.raises(AppError) as err:
        if operation == "create":
            await service.create(pub.campaign_id, config())
        elif operation == "delete":
            await service.delete(kpi.id)
        else:
            await service.update(
                kpi.id,
                KPIUpdate(is_active=False)
                if operation == "deactivate"
                else KPIUpdate(target_value=20),
            )
    assert err.value.status_code == 409


@pytest.mark.integration
async def test_fk_restrict_and_migration(db_session):
    pub, _ = await setup(db_session)
    await CampaignKPIService(db_session).create(pub.campaign_id, config())
    assert (
        await db_session.scalar(text("SELECT version_num FROM alembic_version")) == "20261009_0028"
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conrelid='campaign_kpis'::regclass AND contype='f'"
            )
        )
        == "r"
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(CampaignKPI(campaign_id=uuid4(), **config().model_dump()))
            await db_session.flush()


@pytest.mark.integration
async def test_auth_and_crud_api(client, db_session):
    pub, user = await setup(db_session)
    path = f"/api/v1/campaigns/{pub.campaign_id}/kpis"
    assert (await client.get(path)).status_code == 401
    assert (await client.post(path, json=config().model_dump(mode="json"))).status_code == 401
    assert (await client.patch(f"/api/v1/campaign-kpis/{uuid4()}", json={})).status_code == 401
    assert (await client.delete(f"/api/v1/campaign-kpis/{uuid4()}")).status_code == 401
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    response = await client.post(path, json=config().model_dump(mode="json"))
    assert response.status_code == 201, response.text
    kid = response.json()["id"]
    assert len((await client.get(path)).json()) == 1
    assert (
        await client.patch(
            f"/api/v1/campaign-kpis/{kid}", json={"target_value": "0", "channel": None}
        )
    ).status_code == 200
    assert (await client.delete(f"/api/v1/campaign-kpis/{kid}")).status_code == 204
    assert (await client.get(path)).json() == []
    assert (await client.get("/api/v1/campaigns/" + str(uuid4()) + "/kpis")).status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize(
    "metric,raw,target,comparison,expected,met",
    [
        ("VIEWS", {"views": 12}, "10", "GTE", "12", True),
        ("VIEWS", {"views": 12}, "20", "GTE", "12", False),
        ("CLICKS", {"clicks": 0}, "0", "LTE", "0", True),
        ("LIKES", {}, "0", "GTE", None, None),
        ("CTR", {"clicks": 5, "impressions": 100}, ".1", "LTE", ".05", True),
        (
            "ENGAGEMENT_RATE",
            {"reactions": 2, "comments": 3, "shares": 5, "impressions": 100},
            ".1",
            "GTE",
            ".1",
            True,
        ),
    ],
)
async def test_kpi_observed_targets_and_provenance(
    db_session, metric, raw, target, comparison, expected, met
):
    pub, _ = await setup(db_session)
    snap = await snapshot(db_session, pub, **raw)
    await CampaignKPIService(db_session).create(
        pub.campaign_id, config(metric=metric, target_value=target, comparison=comparison)
    )
    result = await MetricsService(db_session).campaign_performance(pub.campaign_id, START, END)
    fact = result["kpis"][0]
    assert fact["observed_value"] == (Decimal(expected) if expected is not None else None)
    assert fact["target_met"] is met
    assert fact["observed_publication_count"] == (1 if expected is not None else 0)
    row = result["publications"][0]
    assert row["content_version_id"] == pub.content_version_id
    assert row["latest_metrics_snapshot_id"] == snap.id
    assert row["source"] == MetricsSource.MANUAL


@pytest.mark.integration
async def test_period_channel_and_no_eligible(db_session):
    pub, _ = await setup(db_session)
    await snapshot(db_session, pub, views=12)
    other = ContentChannel.VK if pub.channel == ContentChannel.TELEGRAM else ContentChannel.TELEGRAM
    service = CampaignKPIService(db_session)
    await service.create(pub.campaign_id, config(channel=other))
    await service.create(
        pub.campaign_id, config(period_start=END, period_end=END + timedelta(days=1))
    )
    result = await MetricsService(db_session).campaign_performance(pub.campaign_id, START, END)
    for fact in result["kpis"]:
        assert fact["eligible_publication_count"] == 0
        assert fact["observed_value"] is None and fact["target_met"] is None
        assert fact["coverage_ratio"] == 0


@pytest.mark.integration
async def test_partial_coverage_zero_null_and_append_only(db_session):
    pub, _ = await setup(db_session)
    snap = await snapshot(db_session, pub, likes=0, clicks=10, impressions=100)
    second = Publication(
        campaign_id=pub.campaign_id,
        content_item_id=pub.content_item_id,
        content_version_id=pub.content_version_id,
        channel=pub.channel,
        status=PublicationStatus.PUBLISHED,
        published_at=START + timedelta(days=3),
        external_id="second",
    )
    db_session.add(second)
    await db_session.commit()
    await snapshot(db_session, second, impressions=900)
    service = CampaignKPIService(db_session)
    await service.create(pub.campaign_id, config(metric="LIKES", target_value=0))
    await service.create(pub.campaign_id, config(metric="CTR", target_value=".1"))
    before = {
        table: await db_session.scalar(text("SELECT count(*) FROM " + table))
        for table in ["tasks", "agent_runs", "publications", "publication_metrics_snapshots"]
    }
    result = await MetricsService(db_session).campaign_performance(pub.campaign_id, START, END)
    assert result["totals"]["likes"] == 0 and result["totals"]["views"] is None
    assert result["metric_coverage"]["likes"] == 0.5
    assert result["derived"]["ctr"] == Decimal(".1")
    for fact in result["kpis"]:
        assert fact["coverage_ratio"] == 0.5
        assert fact["observed_publication_count"] == 1 and fact["eligible_publication_count"] == 2
        assert fact["limitation"]
    for table, count in before.items():
        assert await db_session.scalar(text("SELECT count(*) FROM " + table)) == count
    await db_session.refresh(snap)
    assert snap.likes == 0 and snap.views is None


@pytest.mark.integration
async def test_snapshot_order_cutoff_and_unpublished_excluded(db_session):
    pub, _ = await setup(db_session)
    created = START + timedelta(days=2)
    for sid, observed_at, views in [
        (UUID(int=1), created, 1),
        (UUID(int=2), created, 2),
        (UUID(int=3), END + timedelta(seconds=1), 99),
    ]:
        db_session.add(
            PublicationMetricsSnapshot(
                id=sid,
                publication_id=pub.id,
                channel=pub.channel,
                source=MetricsSource.MANUAL,
                observed_at=observed_at,
                created_at=created,
                views=views,
            )
        )
    await db_session.commit()
    result = await MetricsService(db_session).campaign_performance(pub.campaign_id, START, END)
    assert result["totals"]["views"] == 2
    assert result["publications"][0]["latest_metrics_snapshot_id"] == UUID(int=2)
    pub.status = PublicationStatus.SCHEDULED
    await db_session.commit()
    empty = await MetricsService(db_session).campaign_performance(pub.campaign_id, START, END)
    assert empty["total_published"] == 0 and all(v is None for v in empty["totals"].values())


@pytest.mark.integration
async def test_performance_api_rich_response(client, db_session):
    pub, user = await setup(db_session)
    await snapshot(db_session, pub, views=0)
    client.cookies.set(settings.auth_cookie_name, AuthService.create_access_token(user))
    response = await client.get(
        f"/api/v1/campaigns/{pub.campaign_id}/performance",
        params={"from": START.isoformat(), "to": END.isoformat()},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert (
        data["campaign_id"] == str(pub.campaign_id)
        and data["totals"]["views"] == 0
        and data["totals"]["likes"] is None
    )
    assert data["publications"][0]["metrics"]["views"] == 0
    assert data["publications"][0]["latest_metrics_snapshot_id"]
    assert data["data_quality"]["coverage_ratio"] == 1


@pytest.mark.integration
async def test_kpi_own_period_channel_independent_of_requested_display_period(db_session):
    pub, _ = await setup(db_session)
    await snapshot(db_session, pub, views=12)
    await CampaignKPIService(db_session).create(pub.campaign_id, config(channel=pub.channel))
    result = await MetricsService(db_session).campaign_performance(
        pub.campaign_id, END, END + timedelta(days=1)
    )
    assert result["total_published"] == 0
    assert result["kpis"][0]["observed_value"] == Decimal(12)
    assert result["kpis"][0]["coverage_ratio"] == 1


@pytest.mark.integration
async def test_created_at_order_precedes_id_and_snapshot_presence_is_not_measurement(db_session):
    pub, _ = await setup(db_session)
    when = START + timedelta(days=2)
    first = PublicationMetricsSnapshot(
        id=UUID(int=900),
        publication_id=pub.id,
        channel=pub.channel,
        source=MetricsSource.MANUAL,
        observed_at=when,
        created_at=when,
        views=10,
    )
    latest = PublicationMetricsSnapshot(
        id=UUID(int=100),
        publication_id=pub.id,
        channel=pub.channel,
        source=MetricsSource.MANUAL,
        observed_at=when,
        created_at=when + timedelta(seconds=1),
    )
    db_session.add_all([first, latest])
    await db_session.commit()
    result = await MetricsService(db_session).campaign_performance(pub.campaign_id, START, END)
    assert result["publications"][0]["latest_metrics_snapshot_id"] == latest.id
    assert result["with_metrics"] == 1  # Backward-compatible snapshot presence.
    assert result["data_quality"]["publications_with_any_metrics"] == 0
    assert result["data_quality"]["coverage_ratio"] == 0
    assert all(value is None for value in result["totals"].values())


@pytest.mark.integration
async def test_concurrent_activation_cannot_exceed_five(db_session):
    pub, _ = await setup(db_session)
    service = CampaignKPIService(db_session)
    for _ in range(4):
        await service.create(pub.campaign_id, config())
    a = await service.create(pub.campaign_id, config(is_active=False))
    b = await service.create(pub.campaign_id, config(is_active=False))
    ids = [a.id, b.id]

    async def activate(kid):
        async with async_session_factory() as session:
            try:
                await CampaignKPIService(session).update(kid, KPIUpdate(is_active=True))
                return "ok"
            except AppError as error:
                await session.rollback()
                return error.code

    results = await asyncio.gather(*(activate(kid) for kid in ids))
    assert sorted(results) == ["KPI_LIMIT_REACHED", "ok"]
