from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.user import User
from app.repositories.approvals import ApprovalRepository


class ApprovalService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = ApprovalRepository(session)

    async def create_strategy_approval(
        self, campaign_id: UUID, version: int, snapshot: dict[str, object], agent_id: UUID
    ) -> Approval:
        return await self.repository.create(
            Approval(
                object_type=ApprovalObjectType.CAMPAIGN_STRATEGY,
                object_id=campaign_id,
                subject_version=version,
                status=ApprovalStatus.PENDING,
                requested_by_agent_id=agent_id,
                subject_snapshot=snapshot,
                metadata_={},
            )
        )

    async def get_pending(self, campaign_id: UUID, *, lock: bool = False) -> Approval:
        approval = await self.repository.current_pending(campaign_id, lock=lock)
        if approval is None:
            raise AppError("STRATEGY_APPROVAL_NOT_FOUND", "Согласование стратегии не найдено.", 404)
        return approval

    async def resolve(
        self, approval: Approval, status: ApprovalStatus, reviewer: User, comment: str | None
    ) -> None:
        approval.status = status
        approval.reviewed_by_user_id = reviewer.id
        approval.comment = comment
        approval.resolved_at = datetime.now(UTC)
        await self.session.flush()
