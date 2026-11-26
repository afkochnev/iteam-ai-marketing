import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.publications import retry_publication
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.integrations.telegram import (
    ProviderPublicationResult,
    PublicationProvider,
    TelegramProviderError,
    VKProviderError,
)
from app.models.activity import ActivityLog
from app.models.content import ContentChannel, ContentVersion
from app.models.publication import (
    LegacyPublicationAttempt,
    Publication,
    PublicationClaimEvent,
    PublicationStatus,
)
from app.models.user import User
from app.schemas.publication import PublicationCreate
from app.services.activity_log_service import ActivityLogService
from app.services.publication_service import PublicationService
from app.tests.test_publications import _approved_post
from app.tests.test_vk_canary_retry import failed_canary
from app.workers.telegram_worker import publish_telegram_publication
from app.workers.vk_worker import publish_vk_publication

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Claim:
    id: UUID
    channel: ContentChannel
    token: str
    user: User

    async def deliver(self, provider: PublicationProvider, *, token: str | None = None):
        async with async_session_factory() as session:
            service = PublicationService(session)
            execute = service.execute_telegram
            if self.channel is ContentChannel.VK:
                execute = service.execute_vk
            return await execute(self.id, provider, execution_token=token or self.token)


@pytest.fixture(params=[ContentChannel.TELEGRAM, ContentChannel.VK])
def channel(request) -> ContentChannel:
    return request.param


@pytest.fixture
async def claim(db_session, monkeypatch, channel) -> _Claim:
    monkeypatch.setattr(settings, "telegram_target_chat_id", "@attempt_test")
    monkeypatch.setattr(settings, "vk_owner_id", -987)
    return await _issue_claim(db_session, monkeypatch, channel)


async def _issue_claim(db_session: AsyncSession, monkeypatch, channel: ContentChannel) -> _Claim:
    monkeypatch.setattr(settings, f"{channel.value.lower()}_publishing_enabled", True)
    user, _campaign, post, version = await _approved_post(db_session)
    post.channel = channel
    await db_session.commit()
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(content_item_id=post.id, content_version_id=version.id, channel=channel),
        user,
    )
    await service.approve(publication.id, user)
    await service.claim_for_publish(publication.id, user)
    row = await db_session.get(Publication, publication.id)
    assert row is not None and row.execution_token
    return _Claim(row.id, channel, row.execution_token, user)


async def _claim_event(session: AsyncSession, publication_id: UUID, token: str):
    return (
        await session.scalars(
            select(PublicationClaimEvent).where(
                PublicationClaimEvent.publication_id == publication_id,
                PublicationClaimEvent.claim_digest == hashlib.sha256(token.encode()).hexdigest(),
            )
        )
    ).one()


def _provider(target_id: str | None = "-987"):
    provider = Mock(spec=PublicationProvider)
    provider.publish.return_value = ProviderPublicationResult(
        "314", None, datetime.now(UTC), target_id
    )
    return provider


@asynccontextmanager
async def _in_flight(
    claim: _Claim, error: Exception | None = None, *, target_id: str | None = "-987"
):
    entered, release = asyncio.Event(), asyncio.Event()
    provider = _provider(target_id)

    async def send(*, text, chat_id):
        entered.set()
        await release.wait()
        if error is not None:
            raise error
        return provider.publish.return_value

    provider.publish.side_effect = send
    worker = asyncio.create_task(claim.deliver(provider))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        yield worker, release
    finally:
        release.set()
        await asyncio.wait_for(worker, timeout=10)


async def _recover(session: AsyncSession):
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    assert await PublicationService(session).recover_stuck_publishing(cutoff=cutoff) == 1


async def test_duplicate_valid_token_during_provider_call(claim: _Claim, db_session: AsyncSession):
    event = await _claim_event(db_session, claim.id, claim.token)
    assert await db_session.get(LegacyPublicationAttempt, event.id) is None
    duplicate = _provider()
    async with _in_flight(claim) as (first, release):
        attempt = await db_session.get(LegacyPublicationAttempt, event.id)
        assert attempt is not None and attempt.status == "STARTED"
        started_at = attempt.started_at
        assert started_at is not None and attempt.finished_at is None
        assert attempt.claimed_target_id == (
            "@attempt_test" if claim.channel is ContentChannel.TELEGRAM else "-987"
        )
        assert attempt.external_id is None and attempt.provider_target_id is None
        assert attempt.failure_code is None
        assert await asyncio.wait_for(claim.deliver(duplicate), timeout=10) is None
        duplicate.publish.assert_not_awaited()
        release.set()
        result = await first
    assert result is not None and result.status is PublicationStatus.PUBLISHED
    assert await claim.deliver(duplicate) is None
    duplicate.publish.assert_not_awaited()
    await db_session.refresh(attempt)
    assert attempt.status == "SUCCEEDED" and attempt.external_id == "314"
    assert attempt.provider_target_id == "-987" and attempt.failure_code is None
    assert attempt.started_at == started_at and attempt.finished_at >= started_at
    row = await db_session.get(Publication, claim.id, populate_existing=True)
    assert row is not None and row.external_id == "314"
    events = await db_session.scalars(
        select(ActivityLog.event_type).where(
            ActivityLog.content_item_id == row.content_item_id,
            ActivityLog.event_type.in_(["PUBLICATION_STARTED", "PUBLICATION_PUBLISHED"]),
        )
    )
    assert sorted(events) == ["PUBLICATION_PUBLISHED", "PUBLICATION_STARTED"]


async def test_interrupted_worker_does_not_send_again_after_restart(
    claim: _Claim, db_session: AsyncSession
):
    first = _provider()
    first.publish.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await claim.deliver(first)
    first.publish.assert_awaited_once()

    replay = _provider()
    assert await claim.deliver(replay) is None
    replay.publish.assert_not_awaited()
    event = await _claim_event(db_session, claim.id, claim.token)
    attempt = await db_session.get(LegacyPublicationAttempt, event.id)
    assert attempt is not None and attempt.status == "STARTED"
    await _recover(db_session)
    await db_session.refresh(attempt)
    assert attempt.status == "UNKNOWN"
    assert attempt.failure_code == f"{claim.channel.value}_RECONCILIATION_REQUIRED"
    assert attempt.finished_at is not None and attempt.external_id is None
    row = await db_session.get(Publication, claim.id, populate_existing=True)
    assert row is not None and row.status is PublicationStatus.FAILED
    assert row.failure_code == f"{claim.channel.value}_RECONCILIATION_REQUIRED"
    assert row.external_id is None and row.execution_token is None


async def _receipt_events(db_session: AsyncSession, row: Publication):
    return (
        await db_session.scalars(
            select(ActivityLog).where(
                ActivityLog.content_item_id == row.content_item_id,
                ActivityLog.event_type.in_(
                    ["PUBLICATION_LATE_PROVIDER_RECEIPT", "PUBLICATION_PUBLISHED"]
                ),
            )
        )
    ).all()


@pytest.mark.parametrize("outcome", ["success", "provider_error", "unexpected_error"])
async def test_late_provider_result_preserves_recovered_claim(
    claim: _Claim, db_session: AsyncSession, outcome: str
):
    error = None
    if outcome == "provider_error":
        error_type = (
            VKProviderError if claim.channel is ContentChannel.VK else TelegramProviderError
        )
        error = error_type("RATE_LIMIT", "provider error", retryable=True)
    elif outcome == "unexpected_error":
        error = RuntimeError("Unexpected provider failure")
    async with _in_flight(claim, error) as (worker, release):
        await _recover(db_session)
        release.set()
        assert await worker is None
    row = await db_session.get(Publication, claim.id, populate_existing=True)
    assert row is not None and row.status is PublicationStatus.FAILED
    assert row.failure_code == f"{claim.channel.value}_RECONCILIATION_REQUIRED"
    assert row.external_id is None and row.execution_token is None and row.retry_count == 0
    events = await _receipt_events(db_session, row)
    if outcome == "success":
        (receipt,) = events
        assert receipt.event_type == "PUBLICATION_LATE_PROVIDER_RECEIPT"
        assert receipt.metadata_ == {
            "publication_id": str(row.id),
            "external_id": "314",
            "provider_target_id": "-987",
        }
    else:
        assert not events


async def _authorize_retry(claim: _Claim, monkeypatch: pytest.MonkeyPatch) -> str:
    async with async_session_factory() as session:
        await _recover(session)
        await PublicationService(session).reconcile_not_published(
            claim.id, claim.user, note="Confirmed absent"
        )
        queue = Mock()
        task = publish_telegram_publication
        if claim.channel is ContentChannel.VK:
            task = publish_vk_publication
        monkeypatch.setattr(task, "apply_async", queue)
        await retry_publication(claim.id, claim.user, session)
        row = await session.get(Publication, claim.id, populate_existing=True)
        assert row is not None and row.status is PublicationStatus.PUBLISHING
        assert row.execution_token and row.execution_token != claim.token
        queue.assert_called_once_with(args=[str(row.id), row.execution_token], queue="publication")
        return row.execution_token


async def test_late_success_cannot_finish_a_new_publishing_claim(
    claim: _Claim, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    async with _in_flight(claim, target_id=None) as (old_worker, release):
        retry_token = await _authorize_retry(claim, monkeypatch)
        release.set()
        assert await old_worker is None
    row = await db_session.get(Publication, claim.id, populate_existing=True)
    assert row is not None and row.status is PublicationStatus.PUBLISHING
    assert row.execution_token == retry_token and row.external_id is None
    old_event = await _claim_event(db_session, claim.id, claim.token)
    new_event = await _claim_event(db_session, claim.id, retry_token)
    assert new_event.id != old_event.id
    old_attempt = await db_session.get(LegacyPublicationAttempt, old_event.id)
    assert old_attempt is not None and old_attempt.status == "UNKNOWN"
    assert old_attempt.external_id is None and old_attempt.provider_target_id is None
    assert await db_session.get(LegacyPublicationAttempt, new_event.id) is None
    receipts = await _receipt_events(db_session, row)
    (receipt,) = receipts
    assert receipt.event_type == "PUBLICATION_LATE_PROVIDER_RECEIPT"
    assert receipt.metadata_ == {"publication_id": str(row.id), "external_id": "314"}
    retried = _provider()
    retried.publish.return_value = ProviderPublicationResult("315", None, datetime.now(UTC))
    await claim.deliver(retried, token=retry_token)
    retried.publish.assert_awaited_once()
    new_attempt = await db_session.get(LegacyPublicationAttempt, new_event.id)
    assert new_attempt is not None and new_attempt.status == "SUCCEEDED"
    assert new_attempt.external_id == "315" and new_attempt.provider_target_id is None
    await db_session.refresh(old_attempt)
    assert old_attempt.status == "UNKNOWN" and old_attempt.external_id is None


async def _legacy_queue(claim: _Claim, db_session: AsyncSession) -> Publication:
    row = await db_session.get(Publication, claim.id)
    assert row is not None
    # Older claims also predate the per-token claim event.
    await db_session.execute(
        delete(PublicationClaimEvent).where(PublicationClaimEvent.publication_id == row.id)
    )
    # The pre-upgrade queue key is per publication and has no token-specific metadata.
    await db_session.execute(
        update(ActivityLog)
        .where(
            ActivityLog.content_item_id == row.content_item_id,
            ActivityLog.event_type == "PUBLICATION_QUEUED",
        )
        .values(
            operation_key=f"publication-queued:{row.id}", metadata_={"publication_id": str(row.id)}
        )
    )
    await db_session.commit()
    return row


async def test_legacy_queue_without_start_can_deliver_once(claim: _Claim, db_session: AsyncSession):
    await _legacy_queue(claim, db_session)
    provider = _provider()
    async with async_session_factory() as session:
        service = PublicationService(session)
        wrong_channel = service.execute_vk
        if claim.channel is ContentChannel.VK:
            wrong_channel = service.execute_telegram
        assert await wrong_channel(claim.id, provider, execution_token=claim.token) is None
    assert await claim.deliver(provider, token="stale-token") is None
    provider.publish.assert_not_awaited()
    result = await claim.deliver(provider)
    assert result is not None and result.status is PublicationStatus.PUBLISHED
    assert await claim.deliver(provider) is None
    provider.publish.assert_awaited_once()
    assert await db_session.scalar(select(PublicationClaimEvent.id)) is None
    assert await db_session.scalar(select(LegacyPublicationAttempt.claim_event_id)) is None


async def test_legacy_start_requires_reconciliation_before_new_authorized_send(
    claim: _Claim, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    row = await _legacy_queue(claim, db_session)
    await ActivityLogService(db_session).record(
        "PUBLICATION_STARTED",
        operation_key=f"publication-started:{row.id}",
        campaign_id=row.campaign_id,
        content_item_id=row.content_item_id,
        metadata={"publication_id": str(row.id), "attempt": 1},
    )
    await db_session.commit()
    provider = _provider()
    assert await claim.deliver(provider) is None
    provider.publish.assert_not_awaited()

    retry_token = await _authorize_retry(claim, monkeypatch)
    assert await claim.deliver(provider) is None
    provider.publish.assert_not_awaited()
    result = await claim.deliver(provider, token=retry_token)
    assert result is not None and result.status is PublicationStatus.PUBLISHED
    assert await claim.deliver(provider, token=retry_token) is None
    provider.publish.assert_awaited_once()


async def test_stale_approved_row_cannot_issue_second_claim(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    user, _campaign, post, version = await _approved_post(db_session)
    first_session = PublicationService(db_session)
    publication = await first_session.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await first_session.approve(publication.id, user)
    cached = await db_session.get(Publication, publication.id)
    assert cached is not None and cached.status is PublicationStatus.APPROVED
    async with async_session_factory() as winner:
        await PublicationService(winner).claim_for_publish(publication.id, user)
        row = await winner.get(Publication, publication.id)
        assert row is not None and row.execution_token
        winning_token = row.execution_token

    # Keep a strong reference so the first session retains its original ORM snapshot.
    assert cached.status is PublicationStatus.APPROVED
    with pytest.raises(AppError) as rejected:
        await first_session.claim_for_publish(publication.id, user)
    assert rejected.value.code == "PUBLICATION_INVALID_STATE"
    await db_session.rollback()
    persisted = await db_session.get(Publication, publication.id, populate_existing=True)
    assert persisted is not None and persisted.execution_token == winning_token


@pytest.mark.parametrize("outcome", ["refused", "timeout", "network", "untrusted_code"])
async def test_attempt_classifies_provider_outcomes(
    claim: _Claim, db_session: AsyncSession, outcome
):
    channel = claim.channel.value
    errors = VKProviderError if claim.channel is ContentChannel.VK else TelegramProviderError
    code = f"{channel}_PERMISSION_ERROR"
    if outcome == "timeout":
        code = f"{channel}_PROVIDER_TIMEOUT"
    elif outcome == "network":
        code = f"{channel}_PROVIDER_ERROR"
    elif outcome == "untrusted_code":
        code = "unsanitized provider details"
    provider = _provider()
    provider.publish.side_effect = errors(
        code, "private provider text", ambiguous=outcome == "timeout"
    )
    await claim.deliver(provider)
    provider.publish.assert_awaited_once()

    event = await _claim_event(db_session, claim.id, claim.token)
    attempt = await db_session.get(LegacyPublicationAttempt, event.id)
    assert attempt is not None and attempt.finished_at >= attempt.started_at
    assert attempt.external_id is None and attempt.provider_target_id is None
    if outcome == "refused":
        assert (attempt.status, attempt.failure_code) == ("FAILED", code)
    else:
        assert attempt.status == "UNKNOWN"
        assert attempt.failure_code == f"{channel}_RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("target_configured", [True, False])
async def test_target_change_blocks_delivery_before_provider_entry(
    db_session: AsyncSession, channel: ContentChannel, monkeypatch, target_configured: bool
):
    monkeypatch.setattr(
        settings, "telegram_target_chat_id", "@original" if target_configured else None
    )
    monkeypatch.setattr(settings, "vk_owner_id", -987 if target_configured else None)
    claim = await _issue_claim(db_session, monkeypatch, channel)
    if channel is ContentChannel.TELEGRAM:
        monkeypatch.setattr(settings, "telegram_target_chat_id", "@different_test_chat")
    else:
        monkeypatch.setattr(settings, "vk_owner_id", -988)
    provider = _provider()
    result = await claim.deliver(provider)
    assert result is not None and result.failure_code == f"{claim.channel.value}_TARGET_MISMATCH"
    provider.publish.assert_not_awaited()
    event = await _claim_event(db_session, claim.id, claim.token)
    assert await db_session.get(LegacyPublicationAttempt, event.id) is None


async def test_recovery_before_execution_does_not_create_an_attempt(
    claim: _Claim, db_session: AsyncSession
):
    event = await _claim_event(db_session, claim.id, claim.token)
    await _recover(db_session)
    provider = _provider()
    assert await claim.deliver(provider) is None
    provider.publish.assert_not_awaited()
    assert await db_session.get(LegacyPublicationAttempt, event.id) is None


async def test_explicit_vk_canary_retry_links_a_new_attempt(db_session, monkeypatch):
    user, service, row, _, _, old_token = await failed_canary(db_session, monkeypatch)
    original = await _claim_event(db_session, row.id, old_token)
    first = await db_session.get(LegacyPublicationAttempt, original.id)
    assert first is not None and first.status == "FAILED"
    assert first.failure_code == "VK_PERMISSION_ERROR"

    await service.claim_canary_retry(row.id, user)
    retry = await _claim_event(db_session, row.id, row.execution_token)
    assert retry.id != original.id
    assert await db_session.get(LegacyPublicationAttempt, retry.id) is None
    provider = _provider()
    provider.publish.return_value = ProviderPublicationResult(
        "952", None, datetime.now(UTC), "-150574411"
    )
    await service.execute_vk(row.id, provider, execution_token=row.execution_token)
    provider.publish.assert_awaited_once()
    second = await db_session.get(LegacyPublicationAttempt, retry.id)
    assert second is not None and second.status == "SUCCEEDED"
    assert second.external_id == "952" and second.provider_target_id == "-150574411"
    await db_session.refresh(first)
    assert first.status == "FAILED" and first.external_id is None


@pytest.mark.parametrize("interrupted_state", ["STARTED", "SUCCEEDED"])
async def test_interrupted_commit_preserves_only_previously_committed_attempt_state(
    claim: _Claim, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, interrupted_state: str
):
    event = await _claim_event(db_session, claim.id, claim.token)
    provider = _provider()
    async with async_session_factory() as worker:
        commit = worker.commit

        async def interrupt():
            await worker.flush()
            staged = await worker.get(LegacyPublicationAttempt, event.id, populate_existing=True)
            if staged is not None and staged.status == interrupted_state:
                raise RuntimeError("test interruption before commit")
            await commit()

        with monkeypatch.context() as fault:
            fault.setattr(worker, "commit", interrupt)
            service = PublicationService(worker)
            execute = service.execute_telegram
            if claim.channel is ContentChannel.VK:
                execute = service.execute_vk
            with pytest.raises(RuntimeError, match="test interruption before commit"):
                await execute(claim.id, provider, execution_token=claim.token)
        await worker.rollback()

    row = await db_session.get(Publication, claim.id, populate_existing=True)
    assert row is not None and row.status is PublicationStatus.PUBLISHING
    assert row.external_id is None
    attempt = await db_session.get(LegacyPublicationAttempt, event.id, populate_existing=True)
    if interrupted_state == "STARTED":
        provider.publish.assert_not_awaited()
        assert attempt is None
    else:
        provider.publish.assert_awaited_once()
        assert attempt is not None and attempt.status == "STARTED" and attempt.external_id is None
        window = timedelta(seconds=settings.publication_publishing_stale_seconds + 1)
        stale = datetime.now(UTC) - window
        await db_session.execute(
            update(Publication).where(Publication.id == claim.id).values(updated_at=stale)
        )
        await db_session.commit()
        await PublicationService(db_session).recover_one_stuck(claim.id)
        await db_session.refresh(attempt)
        assert attempt.status == "UNKNOWN" and attempt.external_id is None


@pytest.mark.parametrize("field", ["channel", "version"])
async def test_claim_snapshot_mismatch_blocks_provider_entry(claim: _Claim, db_session, field):
    event = await _claim_event(db_session, claim.id, claim.token)
    if field == "channel":
        event.channel = (
            ContentChannel.VK
            if claim.channel is ContentChannel.TELEGRAM
            else ContentChannel.TELEGRAM
        )
    else:
        other = await db_session.scalar(
            select(ContentVersion.id).where(ContentVersion.id != event.content_version_id).limit(1)
        )
        assert other is not None
        event.content_version_id = other
    await db_session.commit()

    provider = _provider()
    result = await claim.deliver(provider)
    assert result is not None and result.status is PublicationStatus.FAILED
    assert result.failure_code == "PUBLICATION_CLAIM_SNAPSHOT_MISMATCH"
    assert await claim.deliver(provider) is None
    provider.publish.assert_not_awaited()
    assert await db_session.get(LegacyPublicationAttempt, event.id) is None
    starts = select(ActivityLog.id).where(ActivityLog.event_type == "PUBLICATION_STARTED")
    assert await db_session.scalar(starts) is None
