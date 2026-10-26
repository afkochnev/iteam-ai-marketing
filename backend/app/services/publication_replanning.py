"""Read-only proposals. A snapshot digest is NOT permission to execute a delivery."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.schemas.publication_replanning import (
    Channel,
    ReplanningPreviewItem,
    ReplanningPreviewRequest,
    ReplanningPreviewResponse,
)


@dataclass(frozen=True)
class ScheduleSnapshot:
    publication_id: UUID
    content_version_id: UUID
    channel: Channel
    status: str
    scheduled_at: datetime | None
    updated_at: datetime
    blocked: bool = False


def preview_replanning(
    request: ReplanningPreviewRequest,
    snapshots: list[ScheduleSnapshot],
    *,
    now: datetime,
) -> ReplanningPreviewResponse:
    by_id = {row.publication_id: row for row in snapshots}
    changes = {change.publication_id: change for change in request.changes}
    items = []
    for change in request.changes:
        source = by_id[change.publication_id]
        blockers: list[str] = []
        warnings: list[str] = []
        if source.status not in {"SCHEDULED", "APPROVED"} or source.blocked:
            blockers.append("Состояние публикации не допускает перепланирование")
        if change.scheduled_at <= now:
            blockers.append("Выберите будущую дату")
        if source.channel != change.channel:
            blockers.append("Сначала адаптируйте и согласуйте текст для нового канала")
        for other in snapshots:
            if other.publication_id == source.publication_id or other.status == "CANCELLED":
                continue
            other_change = changes.get(other.publication_id)
            # Never assume a blocked or already published row will be moved away.
            movable = (
                other.status in {"SCHEDULED", "APPROVED"}
                and not other.blocked
                and other_change is not None
                and other_change.channel == other.channel
                and other_change.scheduled_at > now
            )
            when = other_change.scheduled_at if other_change and movable else other.scheduled_at
            channel = other_change.channel if other_change and movable else other.channel
            if when is None or channel != change.channel:
                continue
            distance = abs((when - change.scheduled_at).total_seconds())
            if distance == 0:
                if "В этом канале уже занят выбранный слот" not in blockers:
                    blockers.append("В этом канале уже занят выбранный слот")
            elif distance < 3600 and "Рядом есть другая публикация в этом канале" not in warnings:
                warnings.append("Рядом есть другая публикация в этом канале")
        items.append(
            ReplanningPreviewItem(
                publication_id=source.publication_id,
                content_version_id=source.content_version_id,
                original_scheduled_at=source.scheduled_at,
                original_channel=source.channel,
                proposed_scheduled_at=change.scheduled_at,
                proposed_channel=change.channel,
                requires_content_approval=source.channel != change.channel,
                blockers=blockers,
                warnings=warnings,
            )
        )
    snapshot = [
        {
            "id": str(row.publication_id),
            "version": str(row.content_version_id),
            "channel": row.channel,
            "status": row.status,
            "scheduled_at": row.scheduled_at.astimezone(UTC).isoformat()
            if row.scheduled_at
            else None,
            "updated_at": row.updated_at.astimezone(UTC).isoformat(),
            "blocked": row.blocked,
        }
        for row in sorted(snapshots, key=lambda row: str(row.publication_id))
    ]
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return ReplanningPreviewResponse(snapshot_version=digest, generated_at=now, items=items)
