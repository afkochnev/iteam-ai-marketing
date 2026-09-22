from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivityLog


class ActivityLogService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        event_type: str,
        *,
        operation_key: str | None = None,
        campaign_id: UUID | None = None,
        task_id: UUID | None = None,
        agent_id: UUID | None = None,
        user_id: UUID | None = None,
        content_item_id: UUID | None = None,
        approval_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActivityLog:
        if operation_key:
            existing = await self.session.scalar(
                select(ActivityLog).where(ActivityLog.operation_key == operation_key)
            )
            if existing:
                return existing
        row = ActivityLog(
            event_type=event_type,
            operation_key=operation_key,
            campaign_id=campaign_id,
            task_id=task_id,
            agent_id=agent_id,
            user_id=user_id,
            content_item_id=content_item_id,
            approval_id=approval_id,
            metadata_=metadata or {},
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            # Two workers may emit the same business event concurrently.  The
            # unique operation key is the durable guard; use a savepoint so a
            # duplicate does not poison the caller's transaction.
            if operation_key:
                existing = await self.session.scalar(
                    select(ActivityLog).where(ActivityLog.operation_key == operation_key)
                )
                if existing:
                    return cast(ActivityLog, existing)
            raise
        return row
