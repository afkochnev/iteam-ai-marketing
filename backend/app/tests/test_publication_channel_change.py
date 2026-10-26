from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.content import approve_content
from app.core.errors import AppError
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import ContentChannel, ContentItem, ContentStatus, ContentVersion
from app.models.publication import Publication, PublicationStatus
from app.schemas.content import ContentApprovalRequest
from app.schemas.publication import PublicationCreate
from app.schemas.publication_channel_change import ChannelChangeConfirm, ChannelChangePrepare
from app.services.publication_channel_change import PublicationChannelChangeService
from app.services.publication_service import PublicationService
from app.tests.test_publication_replanning_confirmation import setup_proposal


async def prepare(session):
    user, campaign, original, _ = await setup_proposal(session)
    payload = ChannelChangePrepare(
        expected_content_version_id=original.content_version_id,
        channel=ContentChannel.VK,
        content=(
            "Перед стратегической сессией согласуйте вопросы, "
            "на которые команда должна найти ответы."
        ),
    )
    service = PublicationChannelChangeService(session)
    prepared = await service.prepare(original.id, payload, user)
    return user, original, service, payload, prepared


async def test_adaptation_has_independent_approval_and_preserves_original(db_session):
    user, original, service, payload, prepared = await prepare(db_session)
    item = await db_session.get(ContentItem, prepared.content_item_id)
    assert item.status is ContentStatus.WAITING_APPROVAL and item.channel is ContentChannel.VK
    source = await db_session.get(Publication, original.id)
    assert source.status is PublicationStatus.SCHEDULED
    assert (await db_session.get(ContentVersion, source.content_version_id)).content == "Post"
    approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_type == ApprovalObjectType.CONTENT_ITEM, Approval.object_id == item.id
        )
    )
    assert approval.status is ApprovalStatus.PENDING
    repeated = await service.prepare(original.id, payload, user)
    assert repeated.content_item_id == item.id
    assert len(list((await db_session.scalars(select(Publication))).all())) == 1


async def test_new_approval_and_explicit_replacement_are_both_required(db_session):
    user, original, service, payload, prepared = await prepare(db_session)
    confirm = ChannelChangeConfirm(
        content_item_id=prepared.content_item_id,
        expected_content_version_id=prepared.content_version_id,
        scheduled_at=datetime.now(UTC) + timedelta(days=4),
        confirmed=True,
    )
    with pytest.raises(AppError) as error:
        await service.confirm(original.id, confirm, user)
    assert error.value.code == "PUBLICATION_CONTENT_NOT_APPROVED"
    await approve_content(prepared.content_item_id, ContentApprovalRequest(), user, db_session)
    with pytest.raises(AppError) as error:
        await PublicationService(db_session).create(
            PublicationCreate(
                content_item_id=prepared.content_item_id,
                content_version_id=prepared.content_version_id,
                channel=payload.channel,
            ),
            user,
        )
    assert error.value.code == "CHANNEL_CHANGE_CONFIRMATION_REQUIRED"
    new_id = await service.confirm(original.id, confirm, user)
    assert await service.confirm(original.id, confirm, user) == new_id
    old = await db_session.get(Publication, original.id)
    new = await db_session.get(Publication, new_id)
    assert old.status is PublicationStatus.CANCELLED
    assert new.status is PublicationStatus.SCHEDULED and new.channel is ContentChannel.VK
    assert new.content_version_id == prepared.content_version_id
    assert new.external_id is None and old.external_id is None
    assert len(list((await db_session.scalars(select(Publication))).all())) == 2


async def test_original_state_change_prevents_switch(db_session):
    user, original, service, _, prepared = await prepare(db_session)
    await approve_content(prepared.content_item_id, ContentApprovalRequest(), user, db_session)
    await PublicationService(db_session).cancel(original.id, user)
    with pytest.raises(AppError) as error:
        await service.confirm(
            original.id,
            ChannelChangeConfirm(
                content_item_id=prepared.content_item_id,
                expected_content_version_id=prepared.content_version_id,
                scheduled_at=datetime.now(UTC) + timedelta(days=4),
                confirmed=True,
            ),
            user,
        )
    assert error.value.code == "CHANNEL_CHANGE_BLOCKED"
    assert len(list((await db_session.scalars(select(Publication))).all())) == 1


async def test_concurrent_confirm_creates_one_new_delivery(db_session):
    import asyncio

    from app.core.database import async_session_factory

    user, original, service, _, prepared = await prepare(db_session)
    await approve_content(prepared.content_item_id, ContentApprovalRequest(), user, db_session)
    payload = ChannelChangeConfirm(
        content_item_id=prepared.content_item_id,
        expected_content_version_id=prepared.content_version_id,
        scheduled_at=datetime.now(UTC) + timedelta(days=4),
        confirmed=True,
    )

    async def apply():
        async with async_session_factory() as session:
            return await PublicationChannelChangeService(session).confirm(
                original.id, payload, user
            )

    first, second = await asyncio.gather(apply(), apply())
    assert first == second
    assert len(list((await db_session.scalars(select(Publication))).all())) == 2


async def test_audit_failure_rolls_back_old_cancel_and_new_delivery(db_session, monkeypatch):
    from app.services.activity_log_service import ActivityLogService

    user, original, service, _, prepared = await prepare(db_session)
    await approve_content(prepared.content_item_id, ContentApprovalRequest(), user, db_session)
    source_id, item_id = original.id, prepared.content_item_id
    record = ActivityLogService.record

    async def fail(self, *args, **kwargs):
        if args[0] == "PUBLICATION_CHANNEL_CHANGE_CONFIRMED":
            raise RuntimeError("injected audit failure")
        return await record(self, *args, **kwargs)

    monkeypatch.setattr(ActivityLogService, "record", fail)
    with pytest.raises(RuntimeError):
        await service.confirm(
            source_id,
            ChannelChangeConfirm(
                content_item_id=item_id,
                expected_content_version_id=prepared.content_version_id,
                scheduled_at=datetime.now(UTC) + timedelta(days=4),
                confirmed=True,
            ),
            user,
        )
    await db_session.rollback()
    assert (await db_session.get(Publication, source_id)).status is PublicationStatus.SCHEDULED
    assert not (await db_session.get(ContentItem, item_id)).metadata_["channel_change_applied"]
    assert len(list((await db_session.scalars(select(Publication))).all())) == 1
