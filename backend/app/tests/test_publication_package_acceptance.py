"""Integrated synthetic acceptance: plan -> batch move -> approved channel replacement."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.api.content import approve_content
from app.api.publications import replanning_preview
from app.integrations.telegram import TelegramProvider, VKProvider
from app.models.approval import ApprovalStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication import Publication, PublicationStatus
from app.schemas.content import ContentApprovalRequest
from app.schemas.publication import PublicationCreate
from app.schemas.publication_channel_change import ChannelChangeConfirm, ChannelChangePrepare
from app.schemas.publication_replanning import ReplanningConfirmRequest, ReplanningPreviewRequest
from app.services.approval_service import ApprovalService
from app.services.campaign_workspace_service import CampaignWorkspaceService
from app.services.publication_channel_change import PublicationChannelChangeService
from app.services.publication_replanning_confirmation import confirm_replanning
from app.services.publication_service import PublicationService
from app.tests.test_plan_publication_scheduling import plan_post


async def test_full_package_preserves_plan_and_updates_calendar_and_links(db_session, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Acceptance must never call a provider")

    monkeypatch.setattr(TelegramProvider, "publish", forbidden)
    monkeypatch.setattr(VKProvider, "publish", forbidden)
    user, campaign, post, version, slot = await plan_post(db_session)
    old_date, old_channel, old_text = slot.scheduled_at, slot.channel, version.content
    service = PublicationService(db_session)
    original = await service.schedule_content(post.id, user)
    # A second independently approved post exercises a batch, in the same campaign.
    extra = ContentItem(
        campaign_id=campaign.id,
        source_task_id=post.source_task_id,
        content_type=ContentType.SOCIAL_POST,
        title="Дополнительный пост",
        status=ContentStatus.APPROVED,
        author_agent_id=post.author_agent_id,
        channel=post.channel,
    )
    db_session.add(extra)
    await db_session.flush()
    extra_version = ContentVersion(
        content_item_id=extra.id,
        version_number=1,
        content="Согласуйте ожидаемый результат встречи заранее.",
        structured_content={},
        created_by_user_id=user.id,
    )
    db_session.add(extra_version)
    await db_session.flush()
    extra.current_version_id = extra_version.id
    db_session.add(
        ContentDerivation(
            derived_content_version_id=extra_version.id,
            source_content_version_id=slot.source_content_version_id,
            source_section_key="summary",
        )
    )
    approval = await ApprovalService(db_session).create_content_approval(
        extra.id,
        1,
        {
            "content_item_id": str(extra.id),
            "content_version_id": str(extra_version.id),
            "content_hash": hashlib.sha256(extra_version.content.encode()).hexdigest(),
        },
        None,
    )
    approval.status, approval.resolved_at = ApprovalStatus.APPROVED, datetime.now(UTC)
    await db_session.commit()
    second = await service.create(
        PublicationCreate(
            content_item_id=extra.id, content_version_id=extra_version.id, channel=extra.channel
        ),
        user,
    )
    await service.approve(second.id, user)
    await service.schedule(second.id, old_date + timedelta(days=1), user)
    request = ReplanningPreviewRequest(
        changes=[
            {
                "publication_id": original.id,
                "scheduled_at": old_date + timedelta(days=2),
                "channel": old_channel.value,
            },
            {
                "publication_id": second.id,
                "scheduled_at": old_date + timedelta(days=3),
                "channel": old_channel.value,
            },
        ]
    )
    preview = await replanning_preview(campaign.id, request, user, db_session)
    assert preview.can_confirm_dates
    moved = await confirm_replanning(
        db_session,
        campaign.id,
        ReplanningConfirmRequest(
            **request.model_dump(), snapshot_version=preview.snapshot_version, confirmed=True
        ),
        user,
    )
    source_id = moved.replacements[original.id]
    channel_service = PublicationChannelChangeService(db_session)
    prepared = await channel_service.prepare(
        source_id,
        ChannelChangePrepare(
            expected_content_version_id=version.id,
            channel="TELEGRAM",
            content="Перед встречей согласуйте вопросы и ожидаемые решения команды.",
        ),
        user,
    )
    await approve_content(prepared.content_item_id, ContentApprovalRequest(), user, db_session)
    new_date = old_date + timedelta(days=4)
    final_id = await channel_service.confirm(
        source_id,
        ChannelChangeConfirm(
            content_item_id=prepared.content_item_id,
            expected_content_version_id=prepared.content_version_id,
            scheduled_at=new_date,
            confirmed=True,
        ),
        user,
    )
    responses = await service.list_campaign(campaign.id)
    final = next(row for row in responses if row.id == final_id)
    assert final.publication_plan_item_id == slot.id
    assert final.channel.value == "TELEGRAM" and final.scheduled_at == new_date
    calendar = await service.calendar(
        campaign.id, old_date - timedelta(days=1), new_date + timedelta(days=1)
    )
    active = [row for row in calendar if row.status is PublicationStatus.SCHEDULED]
    assert {row.publication_id for row in active} == {final_id, moved.replacements[second.id]}
    assert next(row for row in active if row.publication_id == final_id).channel.value == "TELEGRAM"
    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)
    current_item = next(item for item in workspace.publication_plan.items if item.id == slot.id)
    assert current_item.social_posts[0].id == prepared.content_item_id
    assert (
        next(stage for stage in current_item.pipeline if stage.label == "Пост").href
        == f"/content/{prepared.content_item_id}"
    )
    assert (
        next(stage for stage in current_item.pipeline if stage.label == "Согласование").href
        == f"/content/{prepared.content_item_id}#approval"
    )
    await db_session.refresh(slot)
    assert slot.scheduled_at == old_date and slot.channel == old_channel
    assert (await db_session.get(ContentVersion, version.id)).content == old_text
    rows = list((await db_session.scalars(select(Publication))).all())
    assert all(row.external_id is None and row.published_at is None for row in rows)
    assert sum(row.status is PublicationStatus.SCHEDULED for row in rows) == 2
