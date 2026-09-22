from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.activity import ActivityLog

router = APIRouter(prefix="/activities", tags=["activities"])


class ActivityResponse(BaseModel):
    id: UUID
    event_type: str
    campaign_id: UUID | None
    task_id: UUID | None
    content_item_id: UUID | None
    approval_id: UUID | None
    metadata: dict[str, Any]
    created_at: datetime


@router.get("", response_model=list[ActivityResponse])
async def list_activities(
    _user: CurrentUser,
    session: SessionDependency,
    campaign_id: UUID | None = None,
) -> list[ActivityResponse]:
    query = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(200)
    if campaign_id:
        query = query.where(ActivityLog.campaign_id == campaign_id)
    rows = list((await session.scalars(query)).all())
    return [
        ActivityResponse(
            id=row.id,
            event_type=row.event_type,
            campaign_id=row.campaign_id,
            task_id=row.task_id,
            content_item_id=row.content_item_id,
            approval_id=row.approval_id,
            metadata=row.metadata_,
            created_at=row.created_at,
        )
        for row in rows
    ]
