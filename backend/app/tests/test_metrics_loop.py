from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import AppError
from app.models.content import ContentChannel
from app.models.marketing_feedback import MarketingFeedbackAnalysis
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.task import Task, TaskType
from app.services.feedback_service import FeedbackService
from app.services.metrics_service import MetricsService
from app.services.performance_analysis_discovery_service import PerformanceAnalysisDiscoveryService
from app.services.performance_evidence import evidence_fingerprint
from app.tests.test_performance_analysis import fixture
from app.tests.test_provider_metrics import metrics_config, telegram, vk  # noqa: E402, F401
from app.workers import metrics_worker


@pytest.mark.integration
async def test_provider_sync_to_normal_automatic_discovery(db_session, monkeypatch, vk):  # noqa: F811
    user, campaign, post, version, analyst = await fixture(db_session, evidence=False)
    post.channel = ContentChannel.VK
    publication = Publication(
        campaign_id=campaign.id,
        content_item_id=post.id,
        content_version_id=version.id,
        channel=ContentChannel.VK,
        status=PublicationStatus.PUBLISHED,
        external_id="964",
        provider_target_id="-150574411",
        published_at=datetime.now(UTC),
    )
    db_session.add(publication)
    await db_session.commit()
    response, request = vk
    response.json = lambda: {
        "response": [{"id": 964, "owner_id": -150574411, "views": {"count": 4}}]
    }
    empty_fp = evidence_fingerprint(await FeedbackService(db_session)._input_snapshot(campaign.id))
    monkeypatch.setattr(settings, "metrics_sync_enabled", True)
    enqueue = Mock()
    monkeypatch.setattr(metrics_worker.sync_publication_metrics, "apply_async", enqueue)
    await metrics_worker._sync_recent()
    assert enqueue.call_count == 1
    assert enqueue.call_args.kwargs == {"args": [str(publication.id)], "queue": "metrics"}
    await metrics_worker._sync_one(publication.id)
    first = await db_session.scalar(select(PublicationMetricsSnapshot))
    assert first.views == 4 and first.likes is None
    first_fp = evidence_fingerprint(await FeedbackService(db_session)._input_snapshot(campaign.id))
    assert first_fp != empty_fp
    # Interleaving a manual snapshot must not invalidate same-provider deduplication.
    await MetricsService(db_session).record_manual(
        publication.id, user, observed_at=datetime.now(UTC), values={"views": 4}
    )
    count = await db_session.scalar(select(func.count(PublicationMetricsSnapshot.id)))
    await metrics_worker._sync_one(publication.id)
    assert await db_session.scalar(select(func.count(PublicationMetricsSnapshot.id))) == count
    response.json = lambda: {
        "response": [{"id": 964, "owner_id": -150574411, "views": {"count": 5}}]
    }
    await metrics_worker._sync_one(publication.id)
    assert await db_session.scalar(select(func.count(PublicationMetricsSnapshot.id))) == count + 1
    changed_fp = evidence_fingerprint(
        await FeedbackService(db_session)._input_snapshot(campaign.id)
    )
    assert changed_fp != first_fp
    await db_session.refresh(first)
    assert first.views == 4
    analyses = await PerformanceAnalysisDiscoveryService(db_session).discover()
    assert len(analyses) == 1
    analysis = await db_session.get(MarketingFeedbackAnalysis, analyses[0])
    task = await db_session.get(Task, analysis.task_id)
    assert task.task_type == TaskType.ANALYZE_PERFORMANCE
    assert task.assigned_agent_id == analyst.id
    assert analysis.trigger_source == "AUTOMATIC" and analysis.status == "DRAFT"
    assert analysis.evidence_fingerprint == changed_fp
    assert await PerformanceAnalysisDiscoveryService(db_session).discover() == []
    assert analysis.agent_run_id is None  # Metrics never invokes Analyst/model directly.


@pytest.mark.integration
async def test_failed_publication_does_not_stop_other_sync_jobs(db_session, monkeypatch, vk):  # noqa: F811
    user, campaign, post, version, analyst = await fixture(db_session, evidence=False)
    post.channel = ContentChannel.VK
    rows = []
    for external in ["964", "965", None]:
        p = Publication(
            campaign_id=campaign.id,
            content_item_id=post.id,
            content_version_id=version.id,
            channel=ContentChannel.VK,
            status=PublicationStatus.PUBLISHED,
            external_id=external,
            provider_target_id="-150574411" if external is not None else None,
            published_at=datetime.now(UTC),
        )
        db_session.add(p)
        rows.append(p)
    await db_session.commit()
    monkeypatch.setattr(settings, "metrics_sync_enabled", True)
    enqueue = Mock()
    monkeypatch.setattr(metrics_worker.sync_publication_metrics, "apply_async", enqueue)
    monkeypatch.setattr(metrics_worker, "report_exception", Mock())
    await metrics_worker._sync_recent()
    assert {x.kwargs["args"][0] for x in enqueue.call_args_list} == {
        str(rows[0].id),
        str(rows[1].id),
    }
    response, post_request = vk
    response.json = lambda: {"error": {"error_code": 5, "error_msg": "PRIVATE_METRICS_SECRET_123"}}
    with pytest.raises(AppError) as error:
        await metrics_worker._sync_one(rows[0].id)
    assert "PRIVATE_METRICS_SECRET_123" not in str(error.value)
    assert await db_session.scalar(select(func.count(PublicationMetricsSnapshot.id))) == 0
    response.json = lambda: {
        "response": [{"id": 965, "owner_id": -150574411, "views": {"count": 0}}]
    }
    await metrics_worker._sync_one(rows[1].id)
    snapshot = await db_session.scalar(select(PublicationMetricsSnapshot))
    assert snapshot.publication_id == rows[1].id and snapshot.views == 0 and snapshot.likes is None


@pytest.mark.integration
async def test_all_null_provider_result_creates_no_evidence(db_session, vk):  # noqa: F811
    _user, campaign, post, version, _analyst = await fixture(db_session, evidence=False)
    post.channel = ContentChannel.VK
    publication = Publication(
        campaign_id=campaign.id,
        content_item_id=post.id,
        content_version_id=version.id,
        channel=ContentChannel.VK,
        status=PublicationStatus.PUBLISHED,
        external_id="964",
        provider_target_id="-150574411",
        published_at=datetime.now(UTC),
    )
    db_session.add(publication)
    await db_session.commit()
    before = await FeedbackService(db_session)._input_snapshot(campaign.id)
    before_fp = evidence_fingerprint(before)
    response, _request = vk
    response.json = lambda: {"response": [{"id": 964, "owner_id": -150574411}]}
    with pytest.raises(AppError) as error:
        await MetricsService(db_session).sync(publication.id)
    assert error.value.code == "METRICS_NO_DATA"
    assert await db_session.scalar(select(func.count(PublicationMetricsSnapshot.id))) == 0
    after = await FeedbackService(db_session)._input_snapshot(campaign.id)
    assert after["metrics_snapshot_ids"] == []
    assert after["metric_coverage_ratio"] == 0
    assert evidence_fingerprint(after) == before_fp


@pytest.mark.integration
async def test_metrics_scheduler_skips_legacy_and_wrong_provider_targets(
    db_session,
    monkeypatch,
    vk,  # noqa: F811
):
    _user, campaign, post, version, _analyst = await fixture(db_session, evidence=False)
    post.channel = ContentChannel.VK
    rows = []
    for external, target in [
        ("964", "-150574411"),
        ("965", None),
        ("966", "-999999"),
    ]:
        row = Publication(
            campaign_id=campaign.id,
            content_item_id=post.id,
            content_version_id=version.id,
            channel=ContentChannel.VK,
            status=PublicationStatus.PUBLISHED,
            external_id=external,
            provider_target_id=target,
            published_at=datetime.now(UTC),
        )
        db_session.add(row)
        rows.append(row)
    await db_session.commit()
    monkeypatch.setattr(settings, "metrics_sync_enabled", True)
    enqueue = Mock()
    monkeypatch.setattr(metrics_worker.sync_publication_metrics, "apply_async", enqueue)
    await metrics_worker._sync_recent()
    assert [call.kwargs["args"][0] for call in enqueue.call_args_list] == [str(rows[0].id)]

    with pytest.raises(AppError) as unbound:
        await MetricsService(db_session).sync(rows[1].id)
    assert unbound.value.code == "METRICS_PROVIDER_TARGET_UNBOUND"

    with pytest.raises(AppError) as mismatch:
        await MetricsService(db_session).sync(rows[2].id)
    assert mismatch.value.code == "METRICS_PROVIDER_TARGET_MISMATCH"


@pytest.mark.integration
async def test_telegram_provider_values_persist_without_fabricated_metrics(db_session, telegram):  # noqa: F811
    from app.tests.test_metrics import _published_publication

    publication, _user = await _published_publication(db_session)
    publication.provider_target_id = "-1001321892281"
    await db_session.commit()
    result = await MetricsService(db_session).sync(publication.id)
    assert result.views == 12 and result.provider == "telegram_mtproto"
    assert result.observed_at.tzinfo == UTC
    assert all(
        getattr(result, field) is None
        for field in [
            "shares",
            "comments",
            "reactions",
            "likes",
            "impressions",
            "clicks",
            "subscribers",
        ]
    )


@pytest.mark.integration
async def test_padded_telegram_target_queues_only_the_configured_chat(
    db_session,
    monkeypatch,
    telegram,  # noqa: F811
):
    from app.tests.test_metrics import _published_publication

    publication, _user = await _published_publication(db_session)
    publication.provider_target_id = "-0001001321892281"
    wrong_rows = []
    for wrong_target in ("0001001321892281", "-0001001321892282", "@iteam2022", None):
        row = Publication(
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            content_version_id=publication.content_version_id,
            channel=publication.channel,
            status=PublicationStatus.PUBLISHED,
            external_id=publication.external_id,
            provider_target_id=wrong_target,
            published_at=publication.published_at,
        )
        db_session.add(row)
        wrong_rows.append(row)
    await db_session.commit()

    enqueue = Mock()
    monkeypatch.setattr(metrics_worker.sync_publication_metrics, "apply_async", enqueue)
    await metrics_worker._sync_recent()
    enqueue.assert_called_once_with(args=[str(publication.id)], queue="metrics")
    for row in wrong_rows:
        with pytest.raises(AppError) as rejected:
            await MetricsService(db_session).sync(row.id)
        assert rejected.value.code == (
            "METRICS_PROVIDER_TARGET_UNBOUND"
            if row.provider_target_id is None
            else "METRICS_PROVIDER_TARGET_MISMATCH"
        )

    snapshot = await MetricsService(db_session).sync(publication.id)
    assert snapshot.views == 12 and snapshot.provider == "telegram_mtproto"
    assert snapshot.external_placement_id is None
    await db_session.refresh(publication)
    assert publication.provider_target_id == "-0001001321892281"


@pytest.mark.integration
async def test_padded_telegram_chat_cannot_borrow_another_placement(
    db_session,
    monkeypatch,
    telegram,  # noqa: F811
):
    from app.tests.test_publication_duplicate_delivery import _issue_claim, _provider

    target_id = "-1001321892281"
    with monkeypatch.context() as publishing:
        publishing.setattr(settings, "telegram_target_chat_id", target_id)
        claim = await _issue_claim(db_session, publishing, ContentChannel.TELEGRAM)
        await claim.deliver(_provider(target_id))
    owner = await db_session.get(Publication, claim.id, populate_existing=True)
    other = Publication(
        campaign_id=owner.campaign_id,
        content_item_id=owner.content_item_id,
        content_version_id=owner.content_version_id,
        channel=owner.channel,
        status=PublicationStatus.PUBLISHED,
        external_id=owner.external_id,
        provider_target_id="-0001001321892281",
        published_at=owner.published_at,
    )
    db_session.add(other)
    await db_session.commit()
    other_id = other.id
    client, _request = telegram
    client.get_messages.return_value.id = int(owner.external_id)
    with pytest.raises(AppError) as conflict:
        await MetricsService(db_session).sync(other_id)
    assert conflict.value.code == "METRICS_PLACEMENT_CONFLICT"
    assert await db_session.scalar(select(PublicationMetricsSnapshot.id)) is None
