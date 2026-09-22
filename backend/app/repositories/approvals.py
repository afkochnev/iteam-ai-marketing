from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus


class ApprovalRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, approval: Approval) -> Approval:
        self.session.add(approval)
        await self.session.flush()
        return approval

    async def get_by_id(self, approval_id: UUID, *, lock: bool = False) -> Approval | None:
        query = select(Approval).where(Approval.id == approval_id)
        if lock:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def current_pending(self, object_id: UUID, *, lock: bool = False) -> Approval | None:
        query = select(Approval).where(
            Approval.object_type == ApprovalObjectType.CAMPAIGN_STRATEGY,
            Approval.object_id == object_id,
            Approval.status == ApprovalStatus.PENDING,
        )
        if lock:
            query = query.with_for_update()
        return (await self.session.execute(query)).scalar_one_or_none()

    async def list_approvals(
        self,
        *,
        status: ApprovalStatus | None = None,
        object_type: ApprovalObjectType | None = None,
        object_id: UUID | None = None,
    ) -> list[Approval]:
        query = select(Approval)
        if status:
            query = query.where(Approval.status == status)
        if object_type:
            query = query.where(Approval.object_type == object_type)
        if object_id:
            query = query.where(Approval.object_id == object_id)
        result = await self.session.execute(
            query.order_by(
                (Approval.status != ApprovalStatus.PENDING),
                Approval.created_at.desc(),
                Approval.id.desc(),
            )
        )
        return list(result.scalars())
