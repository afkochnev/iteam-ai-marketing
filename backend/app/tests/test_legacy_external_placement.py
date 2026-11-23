import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.publications import retry_publication
from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.core.errors import AppError
from app.integrations.telegram import ProviderPublicationResult
from app.models.activity import ActivityLog
from app.models.agent import Agent
from app.models.content import ContentChannel
from app.models.publication import (
    LegacyExternalPlacement,
    LegacyPublicationAttempt,
    Publication,
    PublicationStatus,
)
from app.services.publication_service import PublicationService
from app.tests.test_publication_duplicate_delivery import (
    _Claim,
    _claim_event,
    _in_flight,
    _issue_claim,
    _legacy_queue,
    _provider,
    _recover,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def destinations(monkeypatch):
    monkeypatch.setattr(settings, "telegram_target_chat_id", "@placement_test")
    monkeypatch.setattr(settings, "vk_owner_id", -987)


async def _other_campaign_claim(session, monkeypatch, channel):
    # The shared approved-post fixture seeds these two unique agent slugs per campaign.
    await session.execute(
        update(Agent)
        .where(Agent.slug.in_(("writer", "smm_manager")))
        .values(slug=Agent.slug + "_first")
    )
    return await _issue_claim(session, monkeypatch, channel)


@pytest.mark.parametrize(
    ("second_channel", "second_target"),
    [(ContentChannel.VK, "-987"), (ContentChannel.TELEGRAM, "-988")],
)
async def test_same_object_number_on_another_channel_or_target_is_a_distinct_placement(
    db_session, monkeypatch, second_channel, second_target
):
    first = await _issue_claim(db_session, monkeypatch, ContentChannel.TELEGRAM)
    first_provider = _provider("-987")
    first_published = await first.deliver(first_provider)
    assert first_published.status is PublicationStatus.PUBLISHED
    monkeypatch.setattr(settings, "telegram_target_chat_id", "@second_test_channel")
    second = await _other_campaign_claim(db_session, monkeypatch, second_channel)
    second_provider = _provider(second_target)
    second_published = await second.deliver(second_provider)
    assert second_published.status is PublicationStatus.PUBLISHED

    for claim, provider, target in (
        (first, first_provider, "-987"),
        (second, second_provider, second_target),
    ):
        assert await claim.deliver(provider) is None
        provider.publish.assert_awaited_once()
        claim_event = await _claim_event(db_session, claim.id, claim.token)
        placement = (
            await db_session.scalars(
                select(LegacyExternalPlacement).where(
                    LegacyExternalPlacement.claim_event_id == claim_event.id
                )
            )
        ).one()
        assert (placement.channel, placement.provider_target_id, placement.external_id) == (
            claim.channel,
            target,
            "314",
        )
        assert placement.confirmed_at is not None and placement.confirmed_at.utcoffset() is not None


@pytest.mark.parametrize(
    ("channel", "target", "object_id"),
    [
        (ContentChannel.VK, None, "314"),
        (ContentChannel.VK, "987", "314"),
        (ContentChannel.TELEGRAM, "@placement_test", "314"),
        (ContentChannel.TELEGRAM, "0", "314"),
        (ContentChannel.TELEGRAM, "-987", "0"),
        (ContentChannel.TELEGRAM, "-987", "post314"),
        (ContentChannel.TELEGRAM, "-987", "9223372036854775808"),
    ],
)
async def test_incomplete_or_invalid_receipt_cannot_establish_a_placement(
    db_session, monkeypatch, channel, target, object_id
):
    claim = await _issue_claim(db_session, monkeypatch, channel)
    provider = _provider(target)
    provider.publish.return_value = ProviderPublicationResult(
        object_id, None, datetime.now(UTC), target
    )
    await claim.deliver(provider)
    provider.publish.assert_awaited_once()
    assert await db_session.scalar(select(LegacyExternalPlacement.id)) is None


async def test_legacy_delivery_without_claim_never_mints_placement(db_session, monkeypatch):
    claim = await _issue_claim(db_session, monkeypatch, ContentChannel.TELEGRAM)
    await _legacy_queue(claim, db_session)
    published = await claim.deliver(_provider("-987"))
    assert published.status is PublicationStatus.PUBLISHED
    assert await db_session.scalar(select(LegacyExternalPlacement.id)) is None


async def test_late_receipt_and_manual_confirmation_leave_placement_unassigned(
    db_session, monkeypatch
):
    claim = await _issue_claim(db_session, monkeypatch, ContentChannel.TELEGRAM)
    async with _in_flight(claim) as (delivery, release):
        await _recover(db_session)
        release.set()
        assert await delivery is None
    assert await db_session.scalar(select(LegacyExternalPlacement.id)) is None
    verified = await PublicationService(db_session).reconcile_published(
        claim.id,
        claim.user,
        external_id="314",
        external_url=None,
        published_at=None,
        note="Verified manually",
    )
    assert verified.status is PublicationStatus.PUBLISHED
    assert await db_session.scalar(select(LegacyExternalPlacement.id)) is None


async def _overlap_receipts(observer, first: _Claim, second: _Claim, monkeypatch):
    # Hold the first placement uncommitted until PostgreSQL reports the other writer waiting for it.
    async with (
        async_session_factory() as winner,
        engine.connect() as connection,
        AsyncSession(bind=connection, expire_on_commit=False) as contender,
    ):
        contender_pid = await contender.scalar(select(func.pg_backend_pid()))
        await contender.commit()
        committed = winner.commit
        staged = asyncio.get_running_loop().create_future()
        release = asyncio.Event()

        async def hold_placement():
            await winner.flush()
            if await winner.scalar(select(LegacyExternalPlacement.id)):
                staged.set_result(await winner.scalar(select(func.pg_backend_pid())))
                await release.wait()
            await committed()

        monkeypatch.setattr(winner, "commit", hold_placement)
        first_provider, second_provider = _provider(), _provider()
        first_execution = PublicationService(winner).execute_telegram
        second_execution = PublicationService(contender).execute_telegram
        async with asyncio.timeout(15), asyncio.TaskGroup() as group:
            first_task = group.create_task(
                first_execution(first.id, first_provider, execution_token=first.token)
            )
            first_pid = await staged
            second_task = group.create_task(
                second_execution(second.id, second_provider, execution_token=second.token)
            )
            try:
                blockers = select(func.pg_blocking_pids(contender_pid))
                while first_pid not in await observer.scalar(blockers):
                    await asyncio.sleep(0.02)
            finally:
                release.set()
        first_provider.publish.assert_awaited_once()
        second_provider.publish.assert_awaited_once()
        assert first_task.result().status is PublicationStatus.PUBLISHED
        assert second_task.result().status is PublicationStatus.FAILED


async def test_overlapping_claims_for_one_object_require_operator_reconciliation(
    db_session, monkeypatch
):
    first = await _issue_claim(db_session, monkeypatch, ContentChannel.TELEGRAM)
    second = await _other_campaign_claim(db_session, monkeypatch, ContentChannel.TELEGRAM)
    await _overlap_receipts(db_session, first, second, monkeypatch)
    first_event = await _claim_event(db_session, first.id, first.token)
    second_event = await _claim_event(db_session, second.id, second.token)
    placement = (await db_session.scalars(select(LegacyExternalPlacement))).one()
    assert placement.claim_event_id == first_event.id
    for claim_event in (first_event, second_event):
        attempt = await db_session.get(LegacyPublicationAttempt, claim_event.id)
        assert (attempt.status, attempt.provider_target_id, attempt.external_id) == (
            "SUCCEEDED",
            "-987",
            "314",
        )
    losing_row = await db_session.get(Publication, second.id, populate_existing=True)
    assert losing_row.failure_code == "TELEGRAM_RECONCILIATION_REQUIRED"
    assert losing_row.published_at is None and losing_row.external_id is None
    assert losing_row.retry_count == 0
    audit = (
        await db_session.scalars(
            select(ActivityLog).where(
                ActivityLog.content_item_id == losing_row.content_item_id,
                ActivityLog.event_type.in_(
                    ("PUBLICATION_PUBLISHED", "PUBLICATION_RECONCILIATION_REQUIRED")
                ),
            )
        )
    ).one()
    assert audit.event_type == "PUBLICATION_RECONCILIATION_REQUIRED"
    assert audit.metadata_["reason"] == "EXTERNAL_PLACEMENT_CONFLICT"
    assert audit.metadata_["placement_id"] == str(placement.id)
    assert audit.metadata_["claim_event_id"] == str(second_event.id)
    assert audit.metadata_["provider_target_id"] == "-987"
    assert audit.metadata_["external_id"] == "314"
    replay = _provider()
    assert await second.deliver(replay) is None
    replay.publish.assert_not_awaited()
    with pytest.raises(AppError) as retry:
        await retry_publication(second.id, second.user, db_session)
    assert retry.value.code == "PUBLICATION_NOT_RETRYABLE"
    service = PublicationService(db_session)
    with pytest.raises(AppError) as duplicate:
        await service.reconcile_published(
            second.id,
            second.user,
            external_id="00314",
            external_url=None,
            published_at=None,
            note="Same object",
        )
    assert duplicate.value.code == "PUBLICATION_EXTERNAL_PLACEMENT_CONFLICT"
    corrected = await service.reconcile_published(
        second.id,
        second.user,
        external_id="315",
        external_url=None,
        published_at=None,
        note="Verified separate post",
    )
    assert corrected.status is PublicationStatus.PUBLISHED and corrected.external_id == "315"
    remaining = (await db_session.scalars(select(LegacyExternalPlacement))).one()
    assert remaining.id == placement.id and remaining.claim_event_id == first_event.id


async def test_placement_storage_failure_rolls_back_success_and_blocks_redelivery(
    db_session, monkeypatch
):
    claim = await _issue_claim(db_session, monkeypatch, ContentChannel.TELEGRAM)
    claim_event = await _claim_event(db_session, claim.id, claim.token)

    def fail_insert(conn, cursor, statement, params, context, many):
        if context.isinsert and context.compiled.statement.table.name == "legacy_external_placements":
            raise RuntimeError("injected placement write failure")

    provider = _provider()
    event.listen(engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="injected placement write failure"):
            await claim.deliver(provider)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_insert)
    provider.publish.assert_awaited_once()
    attempt = await db_session.get(LegacyPublicationAttempt, claim_event.id)
    assert attempt.status == "STARTED"
    assert attempt.external_id is None and attempt.finished_at is None
    row = await db_session.get(Publication, claim.id, populate_existing=True)
    assert row.status is PublicationStatus.PUBLISHING
    assert row.external_id is None and row.published_at is None
    assert await db_session.scalar(select(LegacyExternalPlacement.id)) is None
    published = select(ActivityLog.id).where(ActivityLog.event_type == "PUBLICATION_PUBLISHED")
    assert await db_session.scalar(published) is None
    assert await claim.deliver(provider) is None
    provider.publish.assert_awaited_once()
