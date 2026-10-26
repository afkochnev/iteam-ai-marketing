"""Date-only confirmation; exact versions, one transaction, no provider calls."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem, ContentStatus
from app.models.publication import ACTIVE_PUBLICATION_STATUSES, Publication, PublicationStatus
from app.models.publication_plan import PublicationPlan, PublicationPlanStatus
from app.models.user import User
from app.schemas.publication_replanning import ReplanningConfirmRequest, ReplanningConfirmResponse
from app.services.activity_log_service import ActivityLogService
from app.services.publication_replanning import ScheduleSnapshot, preview_replanning
from app.services.publication_service import PublicationService


async def confirm_replanning(
    session: AsyncSession, campaign_id: UUID, payload: ReplanningConfirmRequest, user: User
) -> ReplanningConfirmResponse:
    campaign = await session.get(
        Campaign, campaign_id, with_for_update=True, populate_existing=True
    )
    if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
        raise AppError("CAMPAIGN_NOT_ACTIVE", "Выберите действующую кампанию.", 409)
    digest = hashlib.sha256(
        json.dumps(
            {
                "user": str(user.id),
                "campaign": str(campaign_id),
                "payload": payload.model_dump(mode="json"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    operation_key = f"publication-replanning-confirmed:{digest}"
    previous = await session.scalar(
        select(ActivityLog).where(ActivityLog.operation_key == operation_key)
    )
    if previous is not None:
        return ReplanningConfirmResponse(
            replacements=previous.metadata_["replacements"], already_applied=True
        )
    contents = list(
        (
            await session.scalars(
                select(ContentItem)
                .where(ContentItem.campaign_id == campaign_id)
                .order_by(ContentItem.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    by_content = {item.id: item for item in contents}
    rows = list(
        (
            await session.scalars(
                select(Publication)
                .where(Publication.campaign_id == campaign_id)
                .order_by(Publication.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    by_id = {row.id: row for row in rows}
    if any(change.publication_id not in by_id for change in payload.changes):
        raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена в этой кампании.", 404)
    now = datetime.now(UTC)
    snapshots = [
        ScheduleSnapshot(
            publication_id=row.id,
            content_version_id=row.content_version_id,
            channel=row.channel.value,
            status=row.status.value,
            scheduled_at=row.scheduled_at,
            updated_at=row.updated_at,
            blocked=(
                row.execution_token is not None
                or row.content_item_id not in by_content
                or by_content[row.content_item_id].status is not ContentStatus.APPROVED
                or row.failure_code
                in {"VK_RECONCILIATION_REQUIRED", "TELEGRAM_RECONCILIATION_REQUIRED"}
            ),
        )
        for row in rows
    ]
    preview = preview_replanning(payload, snapshots, now=now)
    if preview.snapshot_version != payload.snapshot_version:
        raise AppError("REPLANNING_STALE", "Расписание изменилось. Повторите предпросмотр.", 409)
    if any(item.blockers for item in preview.items):
        raise AppError("REPLANNING_BLOCKED", "Сначала устраните ограничения предпросмотра.", 409)
    if all(
        by_id[change.publication_id].scheduled_at == change.scheduled_at
        for change in payload.changes
    ):
        raise AppError("REPLANNING_UNCHANGED", "Новых изменений расписания нет.", 409)
    service = PublicationService(session)
    plans = {}
    for change in payload.changes:
        source = by_id[change.publication_id]
        item = await service._ensure_content_version_approved(
            source.content_item_id, source.content_version_id, source.channel
        )
        if any(
            other.id != source.id
            and other.content_item_id == source.content_item_id
            and other.channel == source.channel
            and (
                other.status in ACTIVE_PUBLICATION_STATUSES
                or other.status is PublicationStatus.PUBLISHED
                or other.execution_token is not None
                or other.failure_code
                in {"VK_RECONCILIATION_REQUIRED", "TELEGRAM_RECONCILIATION_REQUIRED"}
            )
            for other in rows
        ):
            raise AppError("PUBLICATION_ALREADY_EXISTS", "У материала есть другая отправка.", 409)
        plan_item = await service._plan_item(item)
        plans[source.id] = plan_item
        if plan_item is not None:
            current = await session.scalar(
                select(PublicationPlan)
                .where(
                    PublicationPlan.campaign_id == campaign_id,
                    PublicationPlan.status == PublicationPlanStatus.APPROVED,
                )
                .order_by(PublicationPlan.created_at.desc(), PublicationPlan.id.desc())
                .limit(1)
            )
            if current is None or current.id != plan_item.publication_plan_id:
                raise AppError("PUBLICATION_PLAN_NOT_CURRENT", "Медиаплан изменился.", 409)
    replacements = {}
    for change in payload.changes:
        source = by_id[change.publication_id]
        if source.scheduled_at == change.scheduled_at:
            continue
        source.status = PublicationStatus.CANCELLED
        await session.flush()
        await ActivityLogService(session).record(
            "PUBLICATION_CANCELLED",
            operation_key=f"publication-cancelled:{source.id}",
            campaign_id=campaign_id,
            user_id=user.id,
            content_item_id=source.content_item_id,
            metadata={"publication_id": str(source.id)},
        )
        replacement = Publication(
            campaign_id=campaign_id,
            content_item_id=source.content_item_id,
            content_version_id=source.content_version_id,
            channel=source.channel,
            status=PublicationStatus.SCHEDULED,
            scheduled_at=change.scheduled_at.astimezone(UTC),
            approved_for_publish_at=now,
            approved_for_publish_by=user.id,
        )
        session.add(replacement)
        await session.flush()
        replacements[source.id] = replacement.id
        plan_item = plans[source.id]
        if plan_item is not None:
            await service._record_plan_schedule_override(
                replacement,
                plan_item,
                user,
                reason="Подтверждённое перепланирование",
                source_publication_id=source.id,
            )
        await ActivityLogService(session).record(
            "PUBLICATION_REPLANNED",
            operation_key=f"publication-replanned:{replacement.id}",
            campaign_id=campaign_id,
            user_id=user.id,
            content_item_id=source.content_item_id,
            metadata={
                "publication_id": str(replacement.id),
                "source_publication_id": str(source.id),
                "content_version_id": str(source.content_version_id),
                "scheduled_at": change.scheduled_at.astimezone(UTC).isoformat(),
            },
        )
    await ActivityLogService(session).record(
        "PUBLICATION_REPLANNING_CONFIRMED",
        operation_key=operation_key,
        campaign_id=campaign_id,
        user_id=user.id,
        metadata={
            "snapshot_version": payload.snapshot_version,
            "replacements": {str(old): str(new) for old, new in replacements.items()},
        },
    )
    await session.commit()
    return ReplanningConfirmResponse(replacements=replacements)
