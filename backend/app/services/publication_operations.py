"""Computed operational metadata; never changes durable publication state."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.models.publication import Publication, PublicationStatus


def operational_fields(publication: Publication, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    overdue = (
        publication.status == PublicationStatus.SCHEDULED
        and publication.scheduled_at is not None
        and publication.scheduled_at
        < now - timedelta(seconds=settings.publication_auto_dispatch_max_lateness_seconds)
    )
    return {
        "is_overdue": overdue,
        "lateness_seconds": max(0, int((now - publication.scheduled_at).total_seconds()))
        if overdue and publication.scheduled_at
        else 0,
    }
