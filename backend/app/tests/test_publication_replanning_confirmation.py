from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.publications import replanning_preview
from app.core.errors import AppError
from app.models.publication import Publication, PublicationStatus
from app.schemas.publication import PublicationCreate
from app.schemas.publication_replanning import ReplanningConfirmRequest, ReplanningPreviewRequest
from app.services.activity_log_service import ActivityLogService
from app.services.publication_replanning_confirmation import confirm_replanning
from app.services.publication_service import PublicationService
from app.tests.test_publications import _approved_post


async def setup_proposal(session):
    user, campaign, post, version = await _approved_post(session)
    service = PublicationService(session)
    original = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(original.id, user)
    await service.schedule(original.id, datetime.now(UTC) + timedelta(days=2), user)
    request = ReplanningPreviewRequest(
        changes=[
            dict(
                publication_id=original.id,
                scheduled_at=datetime.now(UTC) + timedelta(days=3),
                channel=post.channel.value,
            )
        ]
    )
    preview = await replanning_preview(campaign.id, request, user, session)
    payload = ReplanningConfirmRequest(
        **request.model_dump(), snapshot_version=preview.snapshot_version, confirmed=True
    )
    return user, campaign, original, payload


async def test_confirm_preserves_version_and_is_idempotent(db_session):
    user, campaign, original, payload = await setup_proposal(db_session)
    result = await confirm_replanning(db_session, campaign.id, payload, user)
    replacement = await db_session.get(Publication, result.replacements[original.id])
    old = await db_session.get(Publication, original.id)
    assert old.status is PublicationStatus.CANCELLED
    assert replacement.status is PublicationStatus.SCHEDULED
    assert replacement.content_version_id == original.content_version_id
    assert replacement.channel == original.channel
    assert replacement.external_id is None
    repeated = await confirm_replanning(db_session, campaign.id, payload, user)
    assert repeated.already_applied and repeated.replacements == result.replacements
    assert len(list((await db_session.scalars(select(Publication))).all())) == 2


async def test_stale_preview_rejected_without_replacement(db_session):
    user, campaign, original, payload = await setup_proposal(db_session)
    await PublicationService(db_session).cancel(original.id, user)
    with pytest.raises(AppError) as error:
        await confirm_replanning(db_session, campaign.id, payload, user)
    assert error.value.code == "REPLANNING_STALE"
    assert len(list((await db_session.scalars(select(Publication))).all())) == 1


async def test_failure_rolls_back_cancellation_and_replacement(db_session, monkeypatch):
    user, campaign, original, payload = await setup_proposal(db_session)
    original_id = original.id

    original_record = ActivityLogService.record

    async def fail(self, *args, **kwargs):
        if args[0] == "PUBLICATION_REPLANNED":
            raise RuntimeError("injected audit failure")
        return await original_record(self, *args, **kwargs)

    monkeypatch.setattr(ActivityLogService, "record", fail)
    with pytest.raises(RuntimeError):
        await confirm_replanning(db_session, campaign.id, payload, user)
    await db_session.rollback()
    old = await db_session.get(Publication, original_id)
    assert old.status is PublicationStatus.SCHEDULED
    assert len(list((await db_session.scalars(select(Publication))).all())) == 1


async def test_concurrent_same_confirmation_creates_one_replacement(db_session):
    import asyncio

    from app.core.database import async_session_factory

    user, campaign, original, payload = await setup_proposal(db_session)
    campaign_id = campaign.id

    async def apply():
        async with async_session_factory() as session:
            return await confirm_replanning(session, campaign_id, payload, user)

    first, second = await asyncio.gather(apply(), apply())
    assert first.replacements == second.replacements
    assert first.already_applied != second.already_applied
    assert len(list((await db_session.scalars(select(Publication))).all())) == 2


async def test_channel_change_cannot_bypass_new_content_approval(db_session):
    user, campaign, original, payload = await setup_proposal(db_session)
    change = payload.changes[0].model_copy(
        update={"channel": "VK" if original.channel.value == "TELEGRAM" else "TELEGRAM"}
    )
    changed = payload.model_copy(update={"changes": [change]})
    with pytest.raises(AppError) as error:
        await confirm_replanning(db_session, campaign.id, changed, user)
    assert error.value.code == "REPLANNING_BLOCKED"
    assert len(list((await db_session.scalars(select(Publication))).all())) == 1
