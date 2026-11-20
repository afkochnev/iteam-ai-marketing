import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import ContentItem, ContentVersion
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import PublicationPlan, PublicationPlanStatus
from app.models.user import User
from app.services.approval_service import ApprovalService
from app.services.publication_service import PublicationService
from app.tests.test_plan_publication_scheduling import plan_post

pytestmark = pytest.mark.integration


async def _approve_newer_revision(
    session: AsyncSession, post: ContentItem, user: User
) -> ContentVersion:
    version = ContentVersion(
        content_item_id=post.id,
        version_number=2,
        content="Separately approved revised post",
        structured_content={"text_markdown": "Separately approved revised post"},
        created_by_user_id=user.id,
        change_description="Separate approval must not change an existing delivery",
    )
    session.add(version)
    await session.flush()
    approval = await ApprovalService(session).create_content_approval(
        post.id,
        version.version_number,
        {
            "content_item_id": str(post.id),
            "content_version_id": str(version.id),
            "version_number": version.version_number,
            "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
        },
        post.author_agent_id,
    )
    approval.status = ApprovalStatus.APPROVED
    approval.reviewed_by_user_id = user.id
    approval.resolved_at = datetime.now(UTC)
    post.current_version_id = version.id
    await session.commit()
    return version


async def test_scheduled_replan_keeps_exact_version_when_newer_revision_is_approved(
    db_session: AsyncSession,
) -> None:
    user, _campaign, post, first, plan_item = await plan_post(db_session)
    service = PublicationService(db_session)
    original = await service.schedule_content(post.id, user)
    original_time = plan_item.scheduled_at
    second = await _approve_newer_revision(db_session, post, user)
    moved_time = original_time + timedelta(days=2)

    replacement = await service.replan(original.id, moved_time, user)

    assert replacement.id != original.id
    assert replacement.status is PublicationStatus.SCHEDULED
    assert replacement.content_version_id == first.id
    assert replacement.content_version_id != second.id
    assert replacement.channel is original.channel
    assert replacement.scheduled_at == moved_time
    assert replacement.approved_for_publish_by == user.id
    old = await db_session.get(Publication, original.id)
    assert old is not None
    assert old.status is PublicationStatus.CANCELLED
    assert old.content_version_id == first.id
    assert old.scheduled_at == original_time
    assert old.external_id is None and old.execution_token is None
    await db_session.refresh(post)
    assert post.current_version_id == second.id
    await db_session.refresh(plan_item)
    assert plan_item.scheduled_at == original_time
    plan = await db_session.get(PublicationPlan, plan_item.publication_plan_id)
    assert plan is not None and plan.status is PublicationPlanStatus.APPROVED
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 2
    event = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-replanned:{replacement.id}"
        )
    )
    assert event is not None
    assert event.metadata_["source_publication_id"] == str(original.id)
    assert event.metadata_["content_version_id"] == str(first.id)
    override = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-plan-slot-rescheduled:{replacement.id}"
        )
    )
    assert override is not None
    assert override.metadata_["content_version_id"] == str(first.id)
    assert override.metadata_["source_publication_id"] == str(original.id)
    assert override.metadata_["original_scheduled_at"] == original_time.astimezone(UTC).isoformat()
    assert override.metadata_["effective_scheduled_at"] == moved_time.astimezone(UTC).isoformat()


async def test_cancelled_replan_still_uses_separately_approved_new_revision(
    db_session: AsyncSession,
) -> None:
    user, _campaign, post, first, plan_item = await plan_post(db_session)
    service = PublicationService(db_session)
    original = await service.schedule_content(post.id, user)
    await service.cancel(original.id, user)
    second = await _approve_newer_revision(db_session, post, user)
    moved_time = plan_item.scheduled_at + timedelta(days=2)

    replacement = await service.replan(original.id, moved_time, user)

    assert replacement.id != original.id
    assert replacement.status is PublicationStatus.SCHEDULED
    assert replacement.content_version_id == second.id
    assert replacement.scheduled_at == moved_time
    old = await db_session.get(Publication, original.id)
    assert old is not None
    assert old.status is PublicationStatus.CANCELLED
    assert old.content_version_id == first.id
    assert old.external_id is None and old.published_at is None
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 2
    event = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-replanned:{replacement.id}"
        )
    )
    assert event is not None
    assert event.metadata_["source_publication_id"] == str(original.id)
    assert event.metadata_["content_version_id"] == str(second.id)


async def test_scheduled_replan_rejects_invalid_original_approval_before_cancelling(
    db_session: AsyncSession,
) -> None:
    user, _campaign, post, first, plan_item = await plan_post(db_session)
    service = PublicationService(db_session)
    original = await service.schedule_content(post.id, user)
    original_time = plan_item.scheduled_at
    second = await _approve_newer_revision(db_session, post, user)
    original_approval = await db_session.scalar(
        select(Approval).where(
            Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
            Approval.object_id == post.id,
            Approval.subject_version == first.version_number,
            Approval.status == ApprovalStatus.APPROVED,
        )
    )
    assert original_approval is not None
    original_approval.status = ApprovalStatus.REJECTED
    await db_session.commit()
    events_before = set(
        await db_session.scalars(
            select(ActivityLog.id).where(ActivityLog.content_item_id == post.id)
        )
    )

    with pytest.raises(AppError) as error:
        await service.replan(original.id, original_time + timedelta(days=2), user)

    assert error.value.code == "PUBLICATION_APPROVAL_REQUIRED"
    old = await db_session.get(Publication, original.id)
    assert old is not None
    assert old.status is PublicationStatus.SCHEDULED
    assert old.content_version_id == first.id
    assert old.scheduled_at == original_time
    assert old.execution_token is None
    assert post.current_version_id == second.id
    await db_session.refresh(plan_item)
    assert plan_item.scheduled_at == original_time
    assert await db_session.scalar(select(func.count()).select_from(Publication)) == 1
    events_after = set(
        await db_session.scalars(
            select(ActivityLog.id).where(ActivityLog.content_item_id == post.id)
        )
    )
    assert events_after == events_before
