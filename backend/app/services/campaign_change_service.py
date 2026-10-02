from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem, ContentStatus, ContentType
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.user import User
from app.repositories.campaigns import CampaignRepository
from app.schemas.campaign import (
    CampaignAffectedPublication,
    CampaignChangeImpact,
    CampaignChangeKind,
    CampaignChangePreview,
    CampaignChangeRequest,
    CampaignChangeState,
    CampaignFieldChange,
    CampaignUpdate,
)
from app.services.activity_log_service import ActivityLogService


class CampaignChangeService:
    ADMINISTRATIVE_FIELDS = frozenset({"name", "description"})
    STRATEGIC_FIELDS = frozenset(
        {
            "goal",
            "product",
            "target_audience",
            "offer",
            "desired_result",
            "start_date",
            "end_date",
        }
    )
    FIELD_LABELS = {
        "name": "Название",
        "description": "Описание",
        "goal": "Цель",
        "product": "Продукт",
        "target_audience": "Целевая аудитория",
        "offer": "Оффер",
        "desired_result": "Желаемый результат",
        "start_date": "Дата начала",
        "end_date": "Дата окончания",
    }

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = CampaignRepository(session)

    async def preview(self, campaign_id: UUID, payload: CampaignUpdate) -> CampaignChangePreview:
        campaign = await self._campaign(campaign_id)
        return await self._preview(campaign, payload)

    async def apply(
        self,
        campaign_id: UUID,
        request: CampaignChangeRequest,
        user: User,
    ) -> tuple[Campaign, CampaignChangePreview]:
        campaign = await self._locked_campaign(campaign_id)
        if request.expected_updated_at is not None and self._utc(
            request.expected_updated_at
        ) != self._utc(campaign.updated_at):
            raise AppError(
                "CAMPAIGN_CHANGED_SINCE_PREVIEW",
                "Кампания изменилась после предварительной проверки. Обновите данные и повторите.",
                409,
            )
        preview = await self._preview(campaign, request.changes)
        if not preview.changes:
            return campaign, preview
        if preview.requires_confirmation and not request.confirmed_impact:
            raise AppError(
                "CAMPAIGN_STRATEGIC_CHANGE_CONFIRMATION_REQUIRED",
                "Подтвердите влияние изменений на стратегию и медиаплан.",
                409,
            )
        values = request.changes.model_dump(exclude_unset=True)
        start_date = values.get("start_date", campaign.start_date)
        end_date = values.get("end_date", campaign.end_date)
        self._validate_date_range(start_date, end_date)
        actual = {item.field: values[item.field] for item in preview.changes}
        await self.repository.update(campaign, actual)
        event_type = (
            "CAMPAIGN_STRATEGIC_CHANGED"
            if preview.kind is CampaignChangeKind.STRATEGIC
            else "CAMPAIGN_ADMINISTRATIVE_UPDATED"
        )
        await ActivityLogService(self.session).record(
            event_type,
            campaign_id=campaign.id,
            user_id=user.id,
            metadata={
                "kind": preview.kind.value,
                "changes": [item.model_dump(mode="json") for item in preview.changes],
                "comment": request.comment,
                "strategy_version": campaign.strategy_version,
                "impact": preview.impact.model_dump(mode="json"),
            },
        )
        await self.session.commit()
        current = await self._campaign(campaign.id)
        return current, preview

    async def apply_administrative_patch(
        self, campaign_id: UUID, payload: CampaignUpdate, user: User
    ) -> Campaign:
        campaign = await self._locked_campaign(campaign_id)
        preview = await self._preview(campaign, payload)
        if preview.kind is CampaignChangeKind.STRATEGIC and preview.changes:
            raise AppError(
                "CAMPAIGN_STRATEGIC_CHANGE_REQUIRES_PREVIEW",
                "Стратегические вводные изменяются только через предварительную оценку влияния.",
                409,
            )
        if not preview.changes:
            return campaign
        values = payload.model_dump(exclude_unset=True)
        await self.repository.update(
            campaign, {item.field: values[item.field] for item in preview.changes}
        )
        await ActivityLogService(self.session).record(
            "CAMPAIGN_ADMINISTRATIVE_UPDATED",
            campaign_id=campaign.id,
            user_id=user.id,
            metadata={
                "kind": CampaignChangeKind.ADMINISTRATIVE.value,
                "changes": [item.model_dump(mode="json") for item in preview.changes],
                "strategy_version": campaign.strategy_version,
                "impact": preview.impact.model_dump(mode="json"),
            },
        )
        await self.session.commit()
        return await self._campaign(campaign.id)

    async def state(
        self,
        campaign: Campaign,
        *,
        current_plan: PublicationPlan | None = None,
        contents: list[ContentItem] | None = None,
    ) -> CampaignChangeState:
        latest = await self.session.scalar(
            select(ActivityLog)
            .where(
                ActivityLog.campaign_id == campaign.id,
                ActivityLog.event_type == "CAMPAIGN_STRATEGIC_CHANGED",
            )
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .limit(1)
        )
        if latest is None:
            return CampaignChangeState(
                has_pending_strategic_changes=False,
                changed_fields=[],
                changed_at=None,
                changed_by_user_id=None,
                comment=None,
                baseline_strategy_version=None,
                active_strategy_version=campaign.strategy_version,
                plan_requires_review=False,
                affected_article_count=0,
                affected_social_post_count=0,
            )
        metadata = latest.metadata_ or {}
        baseline = int(metadata.get("strategy_version", 0))
        pending = campaign.strategy_version <= baseline
        if contents is None:
            contents = list(
                (
                    await self.session.scalars(
                        select(ContentItem).where(ContentItem.campaign_id == campaign.id)
                    )
                ).all()
            )
        affected = [item for item in contents if item.created_at <= latest.created_at]
        plan_requires_review = False
        if current_plan is not None:
            if pending:
                plan_requires_review = True
            else:
                current_strategy_approval = await self.session.scalar(
                    select(Approval)
                    .where(
                        Approval.object_type == ApprovalObjectType.CAMPAIGN_STRATEGY,
                        Approval.object_id == campaign.id,
                        Approval.subject_version == campaign.strategy_version,
                        Approval.status == ApprovalStatus.APPROVED,
                    )
                    .order_by(Approval.resolved_at.desc().nullslast())
                    .limit(1)
                )
                strategy_approved_at = (
                    current_strategy_approval.resolved_at
                    if current_strategy_approval is not None
                    else None
                )
                plan_requires_review = current_plan.created_at <= (
                    strategy_approved_at or latest.created_at
                )
        return CampaignChangeState(
            has_pending_strategic_changes=pending,
            changed_fields=[
                str(item.get("field"))
                for item in metadata.get("changes", [])
                if isinstance(item, dict) and item.get("field")
            ],
            changed_at=latest.created_at,
            changed_by_user_id=latest.user_id,
            comment=metadata.get("comment"),
            baseline_strategy_version=baseline,
            active_strategy_version=campaign.strategy_version,
            plan_requires_review=plan_requires_review,
            affected_article_count=sum(
                item.content_type is ContentType.ARTICLE for item in affected
            ),
            affected_social_post_count=sum(
                item.content_type is ContentType.SOCIAL_POST for item in affected
            ),
        )

    async def _preview(self, campaign: Campaign, payload: CampaignUpdate) -> CampaignChangePreview:
        if campaign.status is CampaignStatus.ARCHIVED:
            raise AppError("CAMPAIGN_ARCHIVED", "Архивную кампанию нельзя редактировать.", 409)
        values = payload.model_dump(exclude_unset=True)
        start_date = values.get("start_date", campaign.start_date)
        end_date = values.get("end_date", campaign.end_date)
        self._validate_date_range(start_date, end_date)
        changes: list[CampaignFieldChange] = []
        for field, new_value in values.items():
            old_value = getattr(campaign, field)
            if old_value == new_value:
                continue
            kind = (
                CampaignChangeKind.STRATEGIC
                if field in self.STRATEGIC_FIELDS
                else CampaignChangeKind.ADMINISTRATIVE
            )
            changes.append(
                CampaignFieldChange(
                    field=field,
                    label=self.FIELD_LABELS[field],
                    old_value=self._display(old_value),
                    new_value=self._display(new_value),
                    kind=kind,
                )
            )
        kind = (
            CampaignChangeKind.STRATEGIC
            if any(item.kind is CampaignChangeKind.STRATEGIC for item in changes)
            else CampaignChangeKind.ADMINISTRATIVE
        )
        impact = await self._impact(campaign)
        strategic = kind is CampaignChangeKind.STRATEGIC and bool(changes)
        return CampaignChangePreview(
            kind=kind,
            changes=changes,
            impact=impact,
            requires_confirmation=strategic,
            warning=(
                "Эти изменения могут повлиять на утверждённую стратегию и медиаплан."
                if strategic
                else None
            ),
            guarantees=[
                "Текущая утверждённая стратегия не будет изменена автоматически.",
                "Утверждённые статьи и Social Posts сохранят свои версии.",
                "Медиаплан не будет переписан или удалён.",
                (
                    "Запланированные и опубликованные Publications не будут перенесены, "
                    "отменены или изменены."
                ),
            ],
        )

    async def _impact(self, campaign: Campaign) -> CampaignChangeImpact:
        approved_strategy = await self.session.scalar(
            select(Approval)
            .where(
                Approval.object_type == ApprovalObjectType.CAMPAIGN_STRATEGY,
                Approval.object_id == campaign.id,
                Approval.status == ApprovalStatus.APPROVED,
            )
            .order_by(Approval.subject_version.desc(), Approval.resolved_at.desc().nullslast())
            .limit(1)
        )
        plans = list(
            (
                await self.session.scalars(
                    select(PublicationPlan)
                    .where(
                        PublicationPlan.campaign_id == campaign.id,
                        PublicationPlan.status != PublicationPlanStatus.ARCHIVED,
                    )
                    .options(selectinload(PublicationPlan.items))
                    .order_by(PublicationPlan.created_at.desc())
                )
            ).all()
        )
        rank = {
            PublicationPlanStatus.APPROVED: 0,
            PublicationPlanStatus.WAITING_APPROVAL: 1,
            PublicationPlanStatus.DRAFT: 2,
            PublicationPlanStatus.REJECTED: 3,
        }
        plan = (
            min(
                plans,
                key=lambda item: (rank.get(item.status, 9), -item.created_at.timestamp()),
            )
            if plans
            else None
        )
        now = datetime.now(UTC)
        active_items = (
            [item for item in plan.items if item.status is PublicationPlanItemStatus.PLANNED]
            if plan
            else []
        )
        approved_posts = int(
            await self.session.scalar(
                select(func.count(ContentItem.id)).where(
                    ContentItem.campaign_id == campaign.id,
                    ContentItem.content_type == ContentType.SOCIAL_POST,
                    ContentItem.status == ContentStatus.APPROVED,
                )
            )
            or 0
        )
        scheduled = list(
            (
                await self.session.scalars(
                    select(Publication)
                    .where(
                        Publication.campaign_id == campaign.id,
                        Publication.status == PublicationStatus.SCHEDULED,
                    )
                    .order_by(Publication.scheduled_at.asc().nullslast())
                )
            ).all()
        )
        published_count = int(
            await self.session.scalar(
                select(func.count(Publication.id)).where(
                    Publication.campaign_id == campaign.id,
                    Publication.status == PublicationStatus.PUBLISHED,
                )
            )
            or 0
        )
        return CampaignChangeImpact(
            strategy_version=campaign.strategy_version,
            strategy_status=(
                approved_strategy.status.value if approved_strategy else campaign.status.value
            ),
            publication_plan_id=plan.id if plan else None,
            publication_plan_status=plan.status.value if plan else None,
            publication_plan_item_count=len(active_items),
            future_plan_item_count=sum(item.scheduled_at >= now for item in active_items),
            approved_social_post_count=approved_posts,
            scheduled_publication_count=len(scheduled),
            published_publication_count=published_count,
            scheduled_publications=[
                CampaignAffectedPublication(
                    id=item.id, channel=item.channel.value, scheduled_at=item.scheduled_at
                )
                for item in scheduled
            ],
        )

    async def _campaign(self, campaign_id: UUID) -> Campaign:
        campaign = await self.repository.get_by_id(campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        return campaign

    async def _locked_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = await self.session.scalar(
            select(Campaign).where(Campaign.id == campaign_id).with_for_update()
        )
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        return campaign

    @staticmethod
    def _display(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, date | datetime):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _validate_date_range(start_date: date | None, end_date: date | None) -> None:
        if start_date and end_date and start_date > end_date:
            raise AppError(
                "INVALID_CAMPAIGN_DATE_RANGE",
                "Дата окончания не может быть раньше даты начала.",
                422,
            )
