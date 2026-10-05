from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.content import get_content, list_content
from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.content import ContentChannel, ContentDerivation, ContentVersion
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.schemas.publication import PublicationCreate
from app.services.publication_service import PublicationService
from app.tests.test_publications import _approved_post


async def plan_post(session: AsyncSession):
    user, campaign, post, version = await _approved_post(session)
    source_id = await session.scalar(
        select(ContentDerivation.source_content_version_id).where(
            ContentDerivation.derived_content_version_id == version.id
        )
    )
    source = await session.get(ContentVersion, source_id)
    assert source is not None
    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=datetime.now(UTC),
        planning_horizon_end=datetime.now(UTC) + timedelta(days=30),
        created_by_user_id=user.id,
        approved_at=datetime.now(UTC),
        approved_by_user_id=user.id,
    )
    session.add(plan)
    await session.flush()
    item = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=2,
        scheduled_at=datetime.now(UTC) + timedelta(days=5),
        channel=ContentChannel.VK,
        source_content_item_id=source.content_item_id,
        source_content_version_id=source.id,
        topic="Format choice",
        angle="Uncertainty",
        purpose="Decision",
        format="post",
        message_brief="Approved evidence",
        status=PublicationPlanItemStatus.PLANNED,
    )
    session.add(item)
    await session.flush()
    post.channel = ContentChannel.VK
    post.metadata_ = {
        "publication_plan_id": str(plan.id),
        "publication_plan_item_id": str(item.id),
        "source_content_version_id": str(source.id),
    }
    version.generation_key = f"plan-item:{item.id}"
    await session.commit()
    return user, campaign, post, version, item


@pytest.mark.asyncio
async def test_plan_action_authorizes_future_exact_version_and_calendar(db_session: AsyncSession):
    user, campaign, post, version, item = await plan_post(db_session)
    service = PublicationService(db_session)
    result = await service.schedule_content(post.id, user)
    assert result.status is PublicationStatus.SCHEDULED
    assert result.channel is item.channel
    assert result.scheduled_at == item.scheduled_at
    assert result.content_version_id == version.id
    assert result.publication_plan_item_id == item.id
    assert result.approved_for_publish_by == user.id and result.approved_for_publish_at
    row = await db_session.get(Publication, result.id)
    assert row is not None and row.execution_token is None
    events = list(
        await db_session.scalars(
            select(ActivityLog.event_type)
            .where(ActivityLog.content_item_id == post.id)
            .order_by(ActivityLog.created_at)
        )
    )
    assert len(events) == 3
    assert set(events) == {
        "PUBLICATION_CREATED",
        "PUBLICATION_APPROVED",
        "PUBLICATION_SCHEDULED",
    }
    calendar = await service.calendar(
        campaign.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=7)
    )
    assert len(calendar) == 1 and calendar[0].status is PublicationStatus.SCHEDULED
    assert calendar[0].content_version_id == version.id
    with pytest.raises(AppError) as duplicate:
        await service.schedule_content(post.id, user)
    assert duplicate.value.code == "PUBLICATION_ALREADY_EXISTS"
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 1
    detail = await get_content(post.id, user, db_session)
    assert detail.plan_scheduled_at == item.scheduled_at and detail.plan_channel == "VK"
    listed = await list_content(user, db_session, campaign_id=campaign.id)
    assert next(x for x in listed if x.id == post.id).publication_plan_item_id == item.id


@pytest.mark.asyncio
async def test_plan_create_then_approve_retains_authoritative_schedule(db_session: AsyncSession):
    user, _, post, version, item = await plan_post(db_session)
    service = PublicationService(db_session)
    result = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=item.channel
        ),
        user,
    )
    assert result.status is PublicationStatus.DRAFT and result.scheduled_at == item.scheduled_at
    result = await service.approve(result.id, user)
    assert result.status is PublicationStatus.SCHEDULED
    with pytest.raises(AppError):
        await service.claim_for_publish(result.id, user)
    with pytest.raises(AppError) as change:
        await service.schedule(result.id, item.scheduled_at + timedelta(hours=1), user)
    assert change.value.code == "PUBLICATION_PLAN_SCHEDULE_MISMATCH"


@pytest.mark.asyncio
async def test_plan_channel_request_cannot_override(db_session: AsyncSession):
    user, _, post, version, _ = await plan_post(db_session)
    with pytest.raises(AppError) as error:
        await PublicationService(db_session).create(
            PublicationCreate(
                content_item_id=post.id,
                content_version_id=version.id,
                channel=ContentChannel.TELEGRAM,
            ),
            user,
        )
    assert error.value.code == "PUBLICATION_CHANNEL_MISMATCH"
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 0


@pytest.mark.asyncio
async def test_past_plan_has_controlled_error_and_no_delivery(db_session: AsyncSession):
    user, _, post, _, item = await plan_post(db_session)
    item.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await PublicationService(db_session).schedule_content(post.id, user)
    assert error.value.code == "PUBLICATION_PLAN_ITEM_SCHEDULE_IN_PAST"
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["publication_plan_item_id", "publication_plan_id", "all"])
async def test_missing_plan_link_never_falls_back_to_manual(db_session: AsyncSession, missing: str):
    user, _, post, _, _ = await plan_post(db_session)
    post.metadata_ = (
        {} if missing == "all" else {k: v for k, v in post.metadata_.items() if k != missing}
    )
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await PublicationService(db_session).schedule_content(post.id, user)
    assert error.value.code == "PUBLICATION_PLAN_LINK_INVALID"


@pytest.mark.asyncio
async def test_future_dispatch_and_worker_never_send_then_due_dispatch_once(
    db_session: AsyncSession, monkeypatch
):
    from app.core.config import settings
    from app.services import publication_service
    from app.workers import dispatcher_worker

    user, _, post, _, item = await plan_post(db_session)
    result = await PublicationService(db_session).schedule_content(post.id, user)
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    queued = []
    monkeypatch.setattr(
        dispatcher_worker.publish_vk_publication,
        "apply_async",
        lambda *, args, queue: queued.append(tuple(args)),
    )
    await dispatcher_worker._dispatch_publications()
    assert queued == []

    # Provider fake deliberately raises if called before due time.
    class NoProvider:
        async def publish(self, **kwargs):
            pytest.fail("Future publication reached provider")

    row = await db_session.get(Publication, result.id)
    assert row is not None
    row.status = PublicationStatus.PUBLISHING
    row.execution_token = str(uuid4())
    await db_session.commit()
    assert (
        await PublicationService(db_session).execute_vk(
            row.id, NoProvider(), execution_token=row.execution_token
        )
        is None
    )
    row.status = PublicationStatus.SCHEDULED
    row.execution_token = None
    await db_session.commit()
    due = item.scheduled_at + timedelta(seconds=1)

    class DueClock:
        @staticmethod
        def now(tz):
            return due

    monkeypatch.setattr(dispatcher_worker, "datetime", DueClock)
    monkeypatch.setattr(publication_service, "utc_now", lambda: due)
    await dispatcher_worker._dispatch_publications()
    await dispatcher_worker._dispatch_publications()
    assert len(queued) == 1 and queued[0][0] == str(result.id)
    await db_session.refresh(row)
    assert row.scheduled_at == item.scheduled_at and row.status is PublicationStatus.PUBLISHING


@pytest.mark.asyncio
async def test_new_revision_does_not_switch_planned_payload(db_session: AsyncSession):
    user, _, post, version, _ = await plan_post(db_session)
    service = PublicationService(db_session)
    result = await service.schedule_content(post.id, user)
    newer = ContentVersion(
        content_item_id=post.id, version_number=2, content="New draft", structured_content={}
    )
    db_session.add(newer)
    await db_session.flush()
    post.current_version_id = newer.id
    await db_session.commit()
    assert (await service.get(result.id)).content_version_id == version.id
    with pytest.raises(AppError) as error:
        await service.schedule_content(post.id, user)
    assert error.value.code == "PUBLICATION_APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_unauthorized_scheduled_rows_never_dispatch(db_session: AsyncSession, monkeypatch):
    from app.core.config import settings
    from app.workers import dispatcher_worker

    user, _, post, _, item = await plan_post(db_session)
    result = await PublicationService(db_session).schedule_content(post.id, user)
    row = await db_session.get(Publication, result.id)
    assert row is not None
    row.approved_for_publish_by = None
    row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    monkeypatch.setattr(
        dispatcher_worker.publish_vk_publication,
        "apply_async",
        lambda **_options: pytest.fail("Unauthorized enqueue"),
    )
    await dispatcher_worker._dispatch_publications()
    await db_session.refresh(row)
    assert row.status is PublicationStatus.SCHEDULED


@pytest.mark.asyncio
async def test_plan_worker_rechecks_snapshot_and_approved_content_hash(
    db_session: AsyncSession, monkeypatch
):
    import hashlib

    from app.core.config import settings
    from app.models.approval import Approval
    from app.services import publication_service

    user, _, post, version, item = await plan_post(db_session)
    approval = await db_session.scalar(select(Approval).where(Approval.object_id == post.id))
    assert approval is not None
    approval.subject_snapshot = {
        **approval.subject_snapshot,
        "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
    }
    await db_session.commit()
    service = PublicationService(db_session)
    result = await service.schedule_content(post.id, user)
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    monkeypatch.setattr(
        publication_service, "utc_now", lambda: item.scheduled_at + timedelta(seconds=1)
    )
    await service.claim_for_publish(result.id, user)
    row = await db_session.get(Publication, result.id)
    assert row is not None
    # Simulate prohibited in-place content tampering; worker must fail before provider.
    version.content = "Tampered after human approval"
    await db_session.commit()

    class NeverProvider:
        async def publish(self, **kwargs):
            pytest.fail("Altered approved payload reached VK")

    failed = await service.execute_vk(
        result.id, NeverProvider(), execution_token=row.execution_token
    )
    assert failed is not None and failed.status is PublicationStatus.FAILED
    assert failed.content_version_id == version.id


@pytest.mark.asyncio
async def test_legacy_plan_without_schedule_cannot_bypass_due_guard(
    db_session: AsyncSession, monkeypatch
):
    from app.core.config import settings

    user, _, post, _, _ = await plan_post(db_session)
    service = PublicationService(db_session)
    result = await service.schedule_content(post.id, user)
    row = await db_session.get(Publication, result.id)
    assert row is not None
    row.status = PublicationStatus.APPROVED
    row.scheduled_at = None
    await db_session.commit()
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    with pytest.raises(AppError) as error:
        await service.claim_for_publish(result.id, user)
    assert error.value.code == "PUBLICATION_PLAN_SCHEDULE_MISMATCH"
    assert row.execution_token is None


@pytest.mark.asyncio
async def test_api_exact_future_plan_contract(client, db_session: AsyncSession, monkeypatch):
    from app.api.dependencies import get_current_user
    from app.main import app
    from app.services import publication_service

    user, campaign, post, version, item = await plan_post(db_session)
    item.scheduled_at = datetime(2026, 10, 6, 12, tzinfo=UTC)
    await db_session.commit()
    monkeypatch.setattr(publication_service, "utc_now", lambda: datetime(2026, 10, 1, tzinfo=UTC))
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = await client.post(f"/api/v1/publications/plan-content/{post.id}/schedule")
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["status"] == "SCHEDULED" and data["channel"] == "VK"
        assert (
            datetime.fromisoformat(data["scheduled_at"].replace("Z", "+00:00")) == item.scheduled_at
        )
        assert data["content_version_id"] == str(version.id)
        assert data["publication_plan_item_id"] == str(item.id)
        assert data["approved_for_publish_by"] == str(user.id)
        calendar = await client.get(
            f"/api/v1/publications/campaign/{campaign.id}/calendar",
            params={"from": "2026-10-01T00:00:00Z", "to": "2026-10-10T00:00:00Z"},
        )
        assert calendar.status_code == 200 and calendar.json()[0]["publication_id"] == data["id"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_changed_approved_revision_requires_cancel_old_schedule(db_session: AsyncSession):
    from app.models.approval import ApprovalStatus
    from app.services.approval_service import ApprovalService

    user, _, post, _, _ = await plan_post(db_session)
    service = PublicationService(db_session)
    original = await service.schedule_content(post.id, user)
    newer = ContentVersion(
        content_item_id=post.id, version_number=2, content="New approved", structured_content={}
    )
    db_session.add(newer)
    await db_session.flush()
    post.current_version_id = newer.id
    approval = await ApprovalService(db_session).create_content_approval(
        post.id,
        2,
        {"content_item_id": str(post.id), "content_version_id": str(newer.id), "version_number": 2},
        post.author_agent_id,
    )
    approval.status = ApprovalStatus.APPROVED
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await service.schedule_content(post.id, user)
    assert error.value.code == "PUBLICATION_ALREADY_EXISTS"
    assert (await service.get(original.id)).content_version_id != newer.id


@pytest.mark.asyncio
async def test_concurrent_scheduling_creates_one_authorized_delivery(db_session: AsyncSession):
    import asyncio

    from app.core.database import async_session_factory

    user, _, post, _, _ = await plan_post(db_session)
    user_id, post_id = user.id, post.id

    async def schedule_once():
        async with async_session_factory() as session:
            from app.models.user import User

            actor = await session.get(User, user_id)
            assert actor is not None
            return await PublicationService(session).schedule_content(post_id, actor)

    results = await asyncio.gather(schedule_once(), schedule_once(), return_exceptions=True)
    assert len([result for result in results if isinstance(result, AppError)]) == 1
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 1
