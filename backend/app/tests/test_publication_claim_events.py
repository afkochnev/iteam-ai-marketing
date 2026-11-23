import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.publications import publish_now, retry_publication
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationClaimEvent, PublicationStatus
from app.schemas.publication import PublicationCreate
from app.services.publication_service import PublicationService
from app.tests.test_publications import _approved_post
from app.tests.test_vk_canary_retry import failed_canary
from app.workers.telegram_worker import publish_telegram_publication

pytestmark = pytest.mark.integration


@pytest.fixture
async def approved_publication(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    user, _, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=ContentChannel.TELEGRAM
        ),
        user,
    )
    await service.approve(publication.id, user)
    return user, publication


def claim_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.parametrize("broker_unavailable", [False, True])
async def test_only_legacy_queue_is_called_and_event_survives_broker_failure(
    db_session, approved_publication, monkeypatch, broker_unavailable
):
    user, publication = approved_publication
    failure = ConnectionError("test broker unavailable") if broker_unavailable else None
    enqueue = Mock(side_effect=failure)
    monkeypatch.setattr(publish_telegram_publication, "apply_async", enqueue)
    if broker_unavailable:
        with pytest.raises(ConnectionError, match="test broker unavailable"):
            await publish_now(publication.id, user, db_session)
    else:
        result = await publish_now(publication.id, user, db_session)
        assert result.status is PublicationStatus.PUBLISHING

    await db_session.rollback()
    async with async_session_factory() as observer:
        row = await observer.get(Publication, publication.id)
        assert row is not None and row.status is PublicationStatus.PUBLISHING
        assert row.execution_token is not None
        enqueue.assert_called_once_with(
            args=[str(row.id), row.execution_token], queue="publication"
        )
        event = (
            await observer.scalars(
                select(PublicationClaimEvent).where(PublicationClaimEvent.publication_id == row.id)
            )
        ).one()
        assert event.content_version_id == publication.content_version_id
        assert event.channel is ContentChannel.TELEGRAM
        assert event.claim_digest == claim_digest(row.execution_token)
        assert (event.event_type, event.schema_version, event.delivery_owner) == (
            "legacy.social.claimed",
            1,
            "legacy_direct",
        )
        assert event.created_at is not None and event.id is not None


async def test_claim_event_audit_and_authorization_roll_back_together(
    db_session, approved_publication, monkeypatch
):
    user, publication = approved_publication
    publication_id = publication.id
    events = select(PublicationClaimEvent).where(
        PublicationClaimEvent.publication_id == publication_id
    )
    queued = select(ActivityLog).where(
        ActivityLog.content_item_id == publication.content_item_id,
        ActivityLog.event_type == "PUBLICATION_QUEUED",
    )
    enqueue = Mock()
    monkeypatch.setattr(publish_telegram_publication, "apply_async", enqueue)

    async def interrupt_before_commit():
        # All three changes reached PostgreSQL, but a different connection still sees the old state.
        await db_session.flush()
        assert (await db_session.scalars(events)).one()
        assert (await db_session.scalars(queued)).one()
        pending = await db_session.get(Publication, publication_id)
        assert pending.status is PublicationStatus.PUBLISHING and pending.execution_token
        async with async_session_factory() as observer:
            visible = await observer.get(Publication, publication_id)
            assert visible.status is PublicationStatus.APPROVED and visible.execution_token is None
            assert await observer.scalar(events) is None
            assert await observer.scalar(queued) is None
        raise RuntimeError("interrupted before commit")

    with monkeypatch.context() as fault:
        fault.setattr(db_session, "commit", interrupt_before_commit)
        with pytest.raises(RuntimeError, match="interrupted before commit"):
            await publish_now(publication_id, user, db_session)
    await db_session.rollback()
    async with async_session_factory() as observer:
        persisted = await observer.get(Publication, publication_id)
        assert persisted.status is PublicationStatus.APPROVED and persisted.execution_token is None
        assert await observer.scalar(events) is None
        assert await observer.scalar(queued) is None
    enqueue.assert_not_called()


async def test_rejected_claim_leaves_no_claim_event(db_session, approved_publication, monkeypatch):
    user, publication = approved_publication
    monkeypatch.setattr(settings, "telegram_publishing_enabled", False)
    with pytest.raises(AppError) as refused:
        await PublicationService(db_session).claim_for_publish(publication.id, user)
    assert refused.value.code == "PUBLICATION_PROVIDER_DISABLED"
    assert await db_session.scalar(select(PublicationClaimEvent.id)) is None


async def test_storage_rejects_duplicate_claim_identity_and_other_delivery_owners(
    db_session, approved_publication
):
    user, publication = approved_publication
    await PublicationService(db_session).claim_for_publish(publication.id, user)
    existing = (await db_session.scalars(select(PublicationClaimEvent))).one()
    async with async_session_factory() as writer:
        writer.add(
            PublicationClaimEvent(
                publication_id=existing.publication_id,
                content_version_id=existing.content_version_id,
                channel=existing.channel,
                claim_digest=existing.claim_digest,
                event_type=existing.event_type,
                schema_version=existing.schema_version,
                delivery_owner=existing.delivery_owner,
            )
        )
        with pytest.raises(IntegrityError) as duplicate:
            await writer.commit()
        assert duplicate.value.orig.sqlstate == "23505"
        await writer.rollback()
        with pytest.raises(IntegrityError) as owner:
            await writer.execute(
                update(PublicationClaimEvent)
                .where(PublicationClaimEvent.id == existing.id)
                .values(delivery_owner="relay")
            )
        assert owner.value.orig.sqlstate == "23514"
        await writer.rollback()
        assert await writer.scalar(select(func.count()).select_from(PublicationClaimEvent)) == 1


async def test_explicit_retry_adds_a_distinct_event_and_preserves_first_claim(
    db_session, approved_publication, monkeypatch
):
    user, publication = approved_publication
    enqueue = Mock()
    monkeypatch.setattr(publish_telegram_publication, "apply_async", enqueue)
    await publish_now(publication.id, user, db_session)
    first_token = enqueue.call_args.kwargs["args"][1]

    service = PublicationService(db_session)
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    assert await service.recover_stuck_publishing(cutoff=cutoff) == 1
    await service.reconcile_not_published(publication.id, user, note="Confirmed absent in fixture")
    await retry_publication(publication.id, user, db_session)
    second_token = enqueue.call_args.kwargs["args"][1]
    assert first_token != second_token and enqueue.call_count == 2

    async with async_session_factory() as observer:
        digests = list(
            await observer.scalars(
                select(PublicationClaimEvent.claim_digest).where(
                    PublicationClaimEvent.publication_id == publication.id
                )
            )
        )
        assert sorted(digests) == sorted([claim_digest(first_token), claim_digest(second_token)])


async def test_authorized_vk_canary_retry_has_a_separate_claim_event(db_session, monkeypatch):
    user, service, publication, _, version, old_token = await failed_canary(db_session, monkeypatch)
    await service.claim_canary_retry(publication.id, user)
    new_token = publication.execution_token
    assert new_token is not None and new_token != old_token
    with pytest.raises(AppError):
        await service.claim_canary_retry(publication.id, user)
    async with async_session_factory() as observer:
        events = list(
            await observer.scalars(
                select(PublicationClaimEvent).where(
                    PublicationClaimEvent.publication_id == publication.id
                )
            )
        )
        assert len(events) == 2
        assert {event.claim_digest for event in events} == {
            claim_digest(old_token),
            claim_digest(new_token),
        }
        assert all(event.content_version_id == version.id for event in events)
        assert all(event.channel is ContentChannel.VK for event in events)


@pytest.fixture
def claim_event_migration():
    from alembic.script import ScriptDirectory

    directory = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
    return directory, directory.get_revision("20261123_0031").module


async def test_downgrade_preserves_committed_claim_evidence(
    db_session, approved_publication, claim_event_migration
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    user, publication = approved_publication
    await PublicationService(db_session).claim_for_publish(publication.id, user)
    _, migration = claim_event_migration

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="Refusing to drop recorded publication claim events"):
        await connection.run_sync(downgrade)
    await db_session.rollback()
    async with async_session_factory() as observer:
        event = (await observer.scalars(select(PublicationClaimEvent))).one()
        assert event.publication_id == publication.id


async def test_upgrade_preserves_legacy_inflight_claim_without_creating_an_event(
    db_session, approved_publication, claim_event_migration
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    _, publication = approved_publication
    directory, migration = claim_event_migration
    legacy_token = str(uuid4())

    def rehearse(connection):
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            # Rehearse the historical migration against its own schema inside the savepoint.
            newer_revisions = directory.iterate_revisions(
                context.get_current_revision(), migration.revision
            )
            for newer in newer_revisions:
                newer.module.downgrade()
                context.stamp(directory, newer.down_revision)
            migration.downgrade()
            context.stamp(directory, migration.down_revision)
            assert context.get_current_revision() == "20261121_0030"
            table = Publication.__table__
            connection.execute(
                table.update()
                .where(table.c.id == publication.id)
                .values(status=PublicationStatus.PUBLISHING, execution_token=legacy_token)
            )
            read_row = table.select().where(table.c.id == publication.id)
            before = dict(connection.execute(read_row).mappings().one())
            assert before["status"] is PublicationStatus.PUBLISHING
            assert before["execution_token"] == legacy_token

            migration.upgrade()
            context.stamp(directory, migration.revision)
            assert context.get_current_revision() == "20261123_0031"
            assert dict(connection.execute(read_row).mappings().one()) == before
            assert connection.scalar(select(func.count()).select_from(PublicationClaimEvent)) == 0

    connection = await db_session.connection()
    savepoint = await connection.begin_nested()
    try:
        await connection.run_sync(rehearse)
    finally:
        await savepoint.rollback()
