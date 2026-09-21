from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.errors import AppError
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.repositories.approvals import ApprovalRepository
from app.schemas.approval import ApprovalResponse

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalResponse])
async def list_approvals(
    _user: CurrentUser,
    session: SessionDependency,
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    object_type: ApprovalObjectType | None = None,
    object_id: UUID | None = None,
) -> list[Approval]:
    return await ApprovalRepository(session).list_approvals(
        status=approval_status, object_type=object_type, object_id=object_id
    )


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: UUID, _user: CurrentUser, session: SessionDependency
) -> Approval:
    approval = await ApprovalRepository(session).get_by_id(approval_id)
    if approval is None:
        raise AppError("APPROVAL_NOT_FOUND", "Согласование не найдено.", 404)
    return approval
