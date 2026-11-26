from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import AppError
from app.models.campaign import Campaign
from app.models.hypothesis import HypothesisVersion, MarketingHypothesis
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMembership, WorkspaceRole
from app.schemas.hypothesis import (
    CampaignHypothesesResponse,
    CampaignHypothesisItem,
    HypothesisVersionResponse,
)


class CampaignHypothesisService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_for_campaign(
        self, user_id: UUID, campaign_id: UUID, *, limit: int, offset: int
    ) -> CampaignHypothesesResponse:
        workspace_id, as_of = await self._campaign_scope(user_id, campaign_id)
        scope = (
            MarketingHypothesis.workspace_id == workspace_id,
            MarketingHypothesis.campaign_id == campaign_id,
        )
        total = await self.session.scalar(
            select(func.count()).select_from(MarketingHypothesis).where(*scope)
        )
        response = CampaignHypothesesResponse(
            items=[], total=total or 0, limit=limit, offset=offset, as_of=as_of
        )
        if offset >= response.total:
            return response

        page = (
            select(MarketingHypothesis)
            .where(*scope)
            .order_by(MarketingHypothesis.created_at.desc(), MarketingHypothesis.id.desc())
            .limit(limit)
            .offset(offset)
            .subquery()
        )
        root = aliased(MarketingHypothesis, page)
        latest_draft_version_id = (
            select(HypothesisVersion.id)
            .where(
                HypothesisVersion.hypothesis_id == root.id,
                HypothesisVersion.workspace_id == workspace_id,
                HypothesisVersion.campaign_id == campaign_id,
            )
            .order_by(HypothesisVersion.version_number.desc())
            .limit(1)
            .correlate(root)
            .scalar_subquery()
        )
        rows = await self.session.execute(
            select(root, HypothesisVersion)
            .outerjoin(HypothesisVersion, HypothesisVersion.id == latest_draft_version_id)
            .order_by(root.created_at.desc(), root.id.desc())
        )
        response.items = [
            CampaignHypothesisItem(
                id=hypothesis.id,
                created_at=hypothesis.created_at,
                latest_draft_version=HypothesisVersionResponse.model_validate(version)
                if version is not None
                else None,
            )
            for hypothesis, version in rows.tuples()
        ]
        return response

    async def get_version(
        self, user_id: UUID, campaign_id: UUID, hypothesis_id: UUID, version_number: int
    ) -> HypothesisVersionResponse:
        workspace_id, _ = await self._campaign_scope(user_id, campaign_id)
        version = await self.session.scalar(
            select(HypothesisVersion).where(
                HypothesisVersion.workspace_id == workspace_id,
                HypothesisVersion.campaign_id == campaign_id,
                HypothesisVersion.hypothesis_id == hypothesis_id,
                HypothesisVersion.version_number == version_number,
            )
        )
        if version is None:
            raise AppError("HYPOTHESIS_VERSION_NOT_FOUND", "Версия гипотезы не найдена.", 404)
        return HypothesisVersionResponse.model_validate(version)

    async def _campaign_scope(self, user_id: UUID, campaign_id: UUID) -> tuple[UUID, datetime]:
        result = await self.session.execute(
            select(Workspace.id, func.now())
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(
                Workspace.id == DEFAULT_WORKSPACE_ID,
                Workspace.is_default.is_(True),
                Workspace.is_active.is_(True),
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.is_active.is_(True),
                WorkspaceMembership.role.in_(
                    (WorkspaceRole.WORKSPACE_ADMIN, WorkspaceRole.MARKETING_MANAGER)
                ),
            )
        )
        scope = result.one_or_none()
        if scope is None:
            raise AppError("FORBIDDEN", "Недостаточно прав.", 403)
        # Existing campaigns are implicitly scoped to this sole legacy workspace.
        if await self.session.scalar(select(Campaign.id).where(Campaign.id == campaign_id)) is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        workspace_id, as_of = scope
        return workspace_id, as_of
