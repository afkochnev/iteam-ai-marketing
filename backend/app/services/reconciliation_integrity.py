"""Read-only checks for delivery reconciliation consistency.

These checks deliberately report anomalies instead of repairing them.  The
publication and audit tables are operational history and must not be silently
rewritten by a health/status request.
"""

from typing import Any

from sqlalchemy import String, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivityLog
from app.models.publication import Publication, PublicationReconciliation, PublicationStatus

_RECONCILIATION_EVENTS = (
    "PUBLICATION_RECONCILED_PUBLISHED",
    "PUBLICATION_RECONCILED_NOT_PUBLISHED",
)


async def reconciliation_integrity_report(session: AsyncSession) -> dict[str, Any]:
    """Return counts of reconciliation/audit inconsistencies without mutation."""

    activity_publication_id = ActivityLog.metadata_["publication_id"].as_string()
    matching_history = exists(
        select(PublicationReconciliation.id).where(
            cast(PublicationReconciliation.publication_id, String) == activity_publication_id
        )
    )
    activity_without_history = int(
        await session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(
                ActivityLog.event_type.in_(_RECONCILIATION_EVENTS),
                ~matching_history,
            )
        )
        or 0
    )

    not_published_history = exists(
        select(PublicationReconciliation.id).where(
            PublicationReconciliation.publication_id == Publication.id,
            PublicationReconciliation.decision == "CONFIRMED_NOT_PUBLISHED",
        )
    )
    failure_without_history = int(
        await session.scalar(
            select(func.count())
            .select_from(Publication)
            .where(
                Publication.status == PublicationStatus.FAILED,
                Publication.failure_code == "PUBLICATION_RECONCILED_NOT_PUBLISHED",
                ~not_published_history,
            )
        )
        or 0
    )

    matching_event = exists(
        select(ActivityLog.id).where(
            ActivityLog.event_type == "PUBLICATION_RECONCILED_PUBLISHED",
            ActivityLog.metadata_["publication_id"].as_string()
            == cast(PublicationReconciliation.publication_id, String),
        )
    )
    reconciliation_without_event = int(
        await session.scalar(
            select(func.count())
            .select_from(PublicationReconciliation)
            .where(
                PublicationReconciliation.decision == "CONFIRMED_PUBLISHED",
                ~matching_event,
            )
        )
        or 0
    )

    return {
        "activity_without_reconciliation": activity_without_history,
        "reconciled_not_published_without_history": failure_without_history,
        "reconciliation_without_activity": reconciliation_without_event,
        "total": activity_without_history + failure_without_history + reconciliation_without_event,
    }
