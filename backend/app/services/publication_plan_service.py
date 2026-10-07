from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.errors import AppError
from app.models.agent import AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.agents import AgentRepository
from app.schemas.publication_plan import (
    PlanItemInput,
    PublicationCollisionWarning,
    PublicationPlanCreate,
    PublicationPlanGenerateRequest,
    PublicationPlanItemUpdate,
)
from app.services.activity_log_service import ActivityLogService
from app.services.publication_planner_digest import build_article_planning_digest


class PublicationPlanService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def collision_delta_minutes(plan_time: datetime, publication_time: datetime) -> int | None:
        delta_seconds = abs((publication_time - plan_time).total_seconds())
        if delta_seconds > 2 * 60 * 60:
            return None
        return round(delta_seconds / 60)

    @staticmethod
    def planner_model(agent_model: str | None) -> str:
        model = (agent_model or settings.openai_default_model or "").strip()
        if not model or model.lower() in {
            "publication-planner",
            "planner",
            "placeholder",
            "unknown",
        }:
            raise AppError(
                "PUBLICATION_PLANNER_MODEL_INVALID",
                "Для Publication Planner не задана действительная модель.",
                503,
            )
        return model

    async def collision_warnings(
        self, plan: PublicationPlan
    ) -> dict[UUID, list[PublicationCollisionWarning]]:
        publications = list(
            (
                await self.session.scalars(
                    select(Publication).where(
                        Publication.campaign_id == plan.campaign_id,
                        Publication.scheduled_at.is_not(None),
                        Publication.status.in_(
                            (
                                PublicationStatus.SCHEDULED,
                                PublicationStatus.PUBLISHING,
                                PublicationStatus.PUBLISHED,
                            )
                        ),
                    )
                )
            ).all()
        )
        result: dict[UUID, list[PublicationCollisionWarning]] = {}
        for item in plan.items:
            warnings: list[PublicationCollisionWarning] = []
            for publication in publications:
                if publication.channel != item.channel or publication.scheduled_at is None:
                    continue
                delta_minutes = self.collision_delta_minutes(
                    item.scheduled_at, publication.scheduled_at
                )
                if delta_minutes is not None:
                    warnings.append(
                        PublicationCollisionWarning(
                            publication_id=publication.id,
                            channel=publication.channel,
                            scheduled_at=publication.scheduled_at,
                            delta_minutes=delta_minutes,
                        )
                    )
            result[item.id] = warnings
        return result

    async def _approved_article_versions(
        self, campaign_id: UUID
    ) -> dict[UUID, tuple[ContentItem, ContentVersion]]:
        items = list(
            (
                await self.session.scalars(
                    select(ContentItem)
                    .where(
                        ContentItem.campaign_id == campaign_id,
                        ContentItem.content_type == ContentType.ARTICLE,
                        ContentItem.status == ContentStatus.APPROVED,
                    )
                    .options(selectinload(ContentItem.versions))
                )
            ).all()
        )
        result: dict[UUID, tuple[ContentItem, ContentVersion]] = {}
        for item in items:
            approval = await self.session.scalar(
                select(Approval)
                .where(
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                    Approval.object_id == item.id,
                    Approval.status == ApprovalStatus.APPROVED,
                )
                .order_by(Approval.resolved_at.desc().nullslast())
                .limit(1)
            )
            version_id = (
                (approval.subject_snapshot or {}).get("content_version_id") if approval else None
            )
            version = next((v for v in item.versions if str(v.id) == str(version_id)), None)
            if version is not None:
                result[version.id] = (item, version)
        return result

    async def _validate_feedback(self, campaign_id: UUID, analysis_id: UUID | None) -> None:
        if analysis_id is None:
            return
        analysis = await self.session.get(MarketingFeedbackAnalysis, analysis_id)
        if analysis is None or analysis.campaign_id != campaign_id:
            raise AppError(
                "FEEDBACK_ANALYSIS_INVALID", "Анализ обратной связи не принадлежит кампании.", 422
            )
        if analysis.status is not FeedbackAnalysisStatus.ACCEPTED:
            raise AppError(
                "FEEDBACK_ANALYSIS_NOT_ACCEPTED", "Можно использовать только принятый анализ.", 409
            )

    async def create_plan(
        self, campaign_id: UUID, user: User, data: PublicationPlanCreate
    ) -> PublicationPlan:
        await self._validate_feedback(campaign_id, data.feedback_analysis_id)
        approved = await self._approved_article_versions(campaign_id)
        if not approved and data.items:
            raise AppError("APPROVED_ARTICLES_NOT_FOUND", "Нет утверждённых статей для плана.", 409)
        plan = PublicationPlan(
            campaign_id=campaign_id,
            status=PublicationPlanStatus.DRAFT,
            planning_horizon_start=data.planning_horizon_start,
            planning_horizon_end=data.planning_horizon_end,
            timezone_policy=data.timezone_policy,
            created_by_user_id=user.id,
            feedback_analysis_id=data.feedback_analysis_id,
        )
        self.session.add(plan)
        await self.session.flush()
        await self._add_items(plan, data.items, approved)
        await ActivityLogService(self.session).record(
            "PUBLICATION_PLAN_CREATED",
            operation_key=f"publication-plan-created:{plan.id}",
            campaign_id=campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id), "item_count": len(data.items)},
        )
        await self.session.commit()
        await self.session.refresh(plan)
        return await self.get_plan(plan.id)

    async def _add_items(
        self,
        plan: PublicationPlan,
        items: list[PlanItemInput],
        approved: dict[UUID, tuple[ContentItem, ContentVersion]],
    ) -> None:
        seen: set[tuple[datetime, ContentChannel]] = set()
        for position, item in enumerate(items, start=1):
            scheduled = item.scheduled_at.astimezone(UTC)
            if not (plan.planning_horizon_start <= scheduled <= plan.planning_horizon_end):
                raise AppError(
                    "PLAN_ITEM_OUTSIDE_HORIZON", "Пункт находится вне горизонта планирования.", 422
                )
            key = (scheduled, item.channel)
            if key in seen:
                raise AppError("PLAN_DUPLICATE_SLOT", "В плане есть повторяющийся слот.", 422)
            seen.add(key)
            source = approved.get(item.source_content_version_id)
            if source is None:
                raise AppError(
                    "PLAN_SOURCE_NOT_APPROVED",
                    "Источник должен быть утверждённой активной статьёй.",
                    422,
                )
            self.session.add(
                PublicationPlanItem(
                    publication_plan_id=plan.id,
                    position=position,
                    scheduled_at=scheduled,
                    channel=item.channel,
                    source_content_item_id=source[0].id,
                    source_content_version_id=source[1].id,
                    topic=item.topic,
                    angle=item.angle,
                    purpose=item.purpose,
                    format=item.format,
                    message_brief=item.message_brief,
                    source_claim_ids=item.source_claim_ids or None,
                    source_support_summary=item.source_support_summary,
                    status=PublicationPlanItemStatus.PLANNED,
                )
            )

    async def _editable_plan(self, plan_id: UUID) -> PublicationPlan:
        plan = await self.session.scalar(
            select(PublicationPlan).where(PublicationPlan.id == plan_id).with_for_update()
        )
        if plan is None:
            raise AppError("PUBLICATION_PLAN_NOT_FOUND", "План публикаций не найден.", 404)
        if plan.status is not PublicationPlanStatus.DRAFT:
            raise AppError(
                "PUBLICATION_PLAN_IMMUTABLE",
                "Изменять можно только план в статусе DRAFT.",
                409,
            )
        return plan

    async def _validate_plan_items(self, plan: PublicationPlan) -> None:
        approved = await self._approved_article_versions(plan.campaign_id)
        items = list(
            (
                await self.session.scalars(
                    select(PublicationPlanItem)
                    .where(PublicationPlanItem.publication_plan_id == plan.id)
                    .order_by(PublicationPlanItem.position)
                )
            ).all()
        )
        seen: set[tuple[datetime, ContentChannel]] = set()
        for item in items:
            scheduled = item.scheduled_at.astimezone(UTC)
            if not plan.planning_horizon_start <= scheduled <= plan.planning_horizon_end:
                raise AppError(
                    "PLAN_ITEM_OUTSIDE_HORIZON", "Пункт находится вне горизонта планирования.", 422
                )
            slot = (scheduled, item.channel)
            if slot in seen:
                raise AppError("PLAN_DUPLICATE_SLOT", "В плане есть повторяющийся слот.", 422)
            seen.add(slot)
            source = approved.get(item.source_content_version_id)
            if source is None or source[0].id != item.source_content_item_id:
                raise AppError(
                    "PLAN_SOURCE_NOT_APPROVED",
                    "Источник должен быть утверждённой активной статьёй.",
                    422,
                )

    async def add_item(self, plan_id: UUID, item: PlanItemInput, user: User) -> PublicationPlan:
        plan = await self._editable_plan(plan_id)
        approved = await self._approved_article_versions(plan.campaign_id)
        existing = list(
            (
                await self.session.scalars(
                    select(PublicationPlanItem).where(
                        PublicationPlanItem.publication_plan_id == plan.id
                    )
                )
            ).all()
        )
        source = approved.get(item.source_content_version_id)
        if source is None:
            raise AppError(
                "PLAN_SOURCE_NOT_APPROVED",
                "Источник должен быть утверждённой активной статьёй.",
                422,
            )
        self.session.add(
            PublicationPlanItem(
                publication_plan_id=plan.id,
                position=len(existing) + 1,
                scheduled_at=item.scheduled_at.astimezone(UTC),
                channel=item.channel,
                source_content_item_id=source[0].id,
                source_content_version_id=source[1].id,
                topic=item.topic,
                angle=item.angle,
                purpose=item.purpose,
                format=item.format,
                message_brief=item.message_brief,
                source_claim_ids=item.source_claim_ids or None,
                source_support_summary=item.source_support_summary,
                status=PublicationPlanItemStatus.PLANNED,
            )
        )
        await self._validate_plan_items(plan)
        await ActivityLogService(self.session).record(
            "PUBLICATION_PLAN_UPDATED",
            operation_key=f"publication-plan-updated:{plan.id}:add:{item.source_content_version_id}:{item.scheduled_at.isoformat()}",
            campaign_id=plan.campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id), "action": "add_item"},
        )
        await self.session.commit()
        return await self.get_plan(plan.id)

    async def update_item(
        self, plan_id: UUID, item_id: UUID, data: PublicationPlanItemUpdate, user: User
    ) -> PublicationPlan:
        plan = await self._editable_plan(plan_id)
        item = await self.session.scalar(
            select(PublicationPlanItem)
            .where(
                PublicationPlanItem.id == item_id,
                PublicationPlanItem.publication_plan_id == plan.id,
            )
            .with_for_update()
        )
        if item is None:
            raise AppError("PUBLICATION_PLAN_ITEM_NOT_FOUND", "Пункт плана не найден.", 404)
        for field, value in data.model_dump(exclude_unset=True).items():
            if field == "scheduled_at" and value is not None:
                value = value.astimezone(UTC)
            setattr(item, field, value)
        if data.source_content_version_id is not None:
            approved = await self._approved_article_versions(plan.campaign_id)
            source = approved.get(data.source_content_version_id)
            if source is None:
                raise AppError(
                    "PLAN_SOURCE_NOT_APPROVED",
                    "Источник должен быть утверждённой активной статьёй.",
                    422,
                )
            item.source_content_item_id = source[0].id
        await self._validate_plan_items(plan)
        await ActivityLogService(self.session).record(
            "PUBLICATION_PLAN_UPDATED",
            operation_key=f"publication-plan-updated:{plan.id}:edit:{item.id}:{item.updated_at}",
            campaign_id=plan.campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id), "item_id": str(item.id), "action": "edit_item"},
        )
        await self.session.commit()
        return await self.get_plan(plan.id)

    async def remove_item(self, plan_id: UUID, item_id: UUID, user: User) -> PublicationPlan:
        plan = await self._editable_plan(plan_id)
        item = await self.session.scalar(
            select(PublicationPlanItem)
            .where(
                PublicationPlanItem.id == item_id,
                PublicationPlanItem.publication_plan_id == plan.id,
            )
            .with_for_update()
        )
        if item is None:
            raise AppError("PUBLICATION_PLAN_ITEM_NOT_FOUND", "Пункт плана не найден.", 404)
        await self.session.delete(item)
        await self.session.flush()
        await self._normalize_positions(plan.id)
        await ActivityLogService(self.session).record(
            "PUBLICATION_PLAN_UPDATED",
            operation_key=f"publication-plan-updated:{plan.id}:remove:{item_id}",
            campaign_id=plan.campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id), "action": "remove_item"},
        )
        await self.session.commit()
        return await self.get_plan(plan.id)

    async def reorder(self, plan_id: UUID, item_ids: list[UUID], user: User) -> PublicationPlan:
        plan = await self._editable_plan(plan_id)
        items = list(
            (
                await self.session.scalars(
                    select(PublicationPlanItem)
                    .where(PublicationPlanItem.publication_plan_id == plan.id)
                    .with_for_update()
                )
            ).all()
        )
        by_id = {item.id: item for item in items}
        if len(item_ids) != len(items) or set(item_ids) != set(by_id):
            raise AppError(
                "PUBLICATION_PLAN_REORDER_INVALID",
                "Нужно передать все пункты плана ровно один раз.",
                422,
            )
        for temporary_position, item_id in enumerate(item_ids, start=1):
            by_id[item_id].position = -temporary_position
        await self.session.flush()
        for position, item_id in enumerate(item_ids, start=1):
            by_id[item_id].position = position
        await ActivityLogService(self.session).record(
            "PUBLICATION_PLAN_UPDATED",
            operation_key=(
                f"publication-plan-updated:{plan.id}:reorder:{','.join(str(x) for x in item_ids)}"
            ),
            campaign_id=plan.campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id), "action": "reorder"},
        )
        await self.session.commit()
        return await self.get_plan(plan.id)

    async def _normalize_positions(self, plan_id: UUID) -> None:
        items = list(
            (
                await self.session.scalars(
                    select(PublicationPlanItem)
                    .where(PublicationPlanItem.publication_plan_id == plan_id)
                    .order_by(PublicationPlanItem.position, PublicationPlanItem.created_at)
                )
            ).all()
        )
        for position, item in enumerate(items, start=1):
            item.position = position

    async def queue_generation(
        self,
        campaign_id: UUID,
        user: User,
        data: PublicationPlanGenerateRequest,
        *,
        commit: bool = True,
        optimization_action_id: UUID | None = None,
        optimization_proposal_id: UUID | None = None,
        optimization_context: dict[str, Any] | None = None,
    ) -> PublicationPlan:
        await self._validate_feedback(campaign_id, data.feedback_analysis_id)
        if (
            data.planning_horizon_end <= data.planning_horizon_start
            or (data.planning_horizon_end - data.planning_horizon_start).days > 90
        ):
            raise AppError(
                "PLAN_HORIZON_INVALID",
                "Горизонт должен быть положительным и не превышать 90 дней.",
                422,
            )
        approved = await self._approved_article_versions(campaign_id)
        if not approved:
            raise AppError("APPROVED_ARTICLES_NOT_FOUND", "Нет утверждённых статей для плана.", 409)
        agent = await AgentRepository(self.session).get_by_slug("marketing_director")
        if agent is None or agent.status is not AgentStatus.ACTIVE:
            raise AppError(
                "PUBLICATION_PLANNER_UNAVAILABLE", "Publication Planner недоступен.", 409
            )
        task = Task(
            campaign_id=campaign_id,
            task_type=TaskType.MANUAL,
            title="Сформировать план публикаций",
            status=TaskStatus.READY,
            assigned_agent_id=agent.id,
            input_data={"publication_plan": True},
            output_data={},
        )
        self.session.add(task)
        await self.session.flush()
        snapshot = {
            "campaign_id": str(campaign_id),
            "planning_horizon_start": data.planning_horizon_start.isoformat(),
            "planning_horizon_end": data.planning_horizon_end.isoformat(),
            "timezone_policy": data.timezone_policy,
            "channels": [str(c) for c in data.channels],
            "total_items": data.total_items,
            "article_version_ids": [str(version_id) for version_id in approved],
            **(optimization_context or {}),
            "article_digests": [
                build_article_planning_digest(version, item.title)
                for item, version in approved.values()
            ],
            "feedback_analysis_id": str(data.feedback_analysis_id)
            if data.feedback_analysis_id
            else None,
        }
        run = AgentRun(
            agent_id=agent.id,
            task_id=task.id,
            campaign_id=campaign_id,
            status=AgentRunStatus.QUEUED,
            input_data={"publication_plan_snapshot": snapshot},
            model=self.planner_model(agent.model),
            prompt_snapshot="Publication Planner: propose editorial schedule only.",
            prompt_hash="publication-planner",
        )
        self.session.add(run)
        await self.session.flush()
        plan = PublicationPlan(
            campaign_id=campaign_id,
            status=PublicationPlanStatus.DRAFT,
            planning_horizon_start=data.planning_horizon_start,
            planning_horizon_end=data.planning_horizon_end,
            timezone_policy=data.timezone_policy,
            created_by_user_id=user.id,
            generated_by_agent_run_id=run.id,
            feedback_analysis_id=data.feedback_analysis_id,
            optimization_action_id=optimization_action_id,
            optimization_proposal_id=optimization_proposal_id,
        )
        self.session.add(plan)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "PUBLICATION_PLAN_GENERATION_QUEUED",
            operation_key=f"publication-plan-generation:{run.id}",
            campaign_id=campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id), "agent_run_id": str(run.id)},
        )
        if not commit:
            return plan
        await self.session.commit()
        await self.enqueue_generation(plan, run)
        return await self.get_plan(plan.id)

    async def enqueue_generation(self, plan: PublicationPlan, run: AgentRun) -> None:
        campaign_id = plan.campaign_id
        try:
            from app.workers.publication_plan_worker import generate_publication_plan

            generate_publication_plan.apply_async(args=[str(run.id)], queue="ai")
        except Exception as exc:
            await self.session.execute(
                select(PublicationPlan).where(PublicationPlan.id == plan.id).with_for_update()
            )
            plan.status = PublicationPlanStatus.DRAFT
            run.status = AgentRunStatus.FAILED
            run.error_code = "PLAN_ENQUEUE_FAILED"
            run.error_message = "Не удалось поставить генерацию плана в очередь."
            run.completed_at = datetime.now(UTC)
            task = await self.session.get(Task, run.task_id)
            if task is not None:
                task.status = TaskStatus.FAILED
                task.error_message = run.error_message
            await ActivityLogService(self.session).record(
                "PUBLICATION_PLAN_GENERATION_FAILED",
                operation_key=f"publication-plan-generation-failed:{run.id}",
                campaign_id=campaign_id,
                metadata={"plan_id": str(plan.id), "reason": "enqueue_failed"},
            )
            await self.session.commit()
            raise AppError(
                "PLAN_ENQUEUE_FAILED", "Не удалось поставить генерацию плана в очередь.", 503
            ) from exc

    async def get_plan(self, plan_id: UUID) -> PublicationPlan:
        row = await self.session.scalar(
            select(PublicationPlan)
            .where(PublicationPlan.id == plan_id)
            .options(selectinload(PublicationPlan.items))
        )
        if row is None:
            raise AppError("PUBLICATION_PLAN_NOT_FOUND", "План публикаций не найден.", 404)
        return row

    async def list_plans(self, campaign_id: UUID) -> list[PublicationPlan]:
        return list(
            (
                await self.session.scalars(
                    select(PublicationPlan)
                    .where(PublicationPlan.campaign_id == campaign_id)
                    .options(selectinload(PublicationPlan.items))
                    .order_by(PublicationPlan.created_at.desc())
                )
            ).all()
        )

    async def transition(
        self, plan_id: UUID, user: User, target: PublicationPlanStatus
    ) -> PublicationPlan:
        source = await self.session.get(PublicationPlan, plan_id)
        if source is None:
            raise AppError("PUBLICATION_PLAN_NOT_FOUND", "План публикаций не найден.", 404)
        # Serialize approval/replacement against Apply's campaign-first validation.
        await self.session.get(
            Campaign, source.campaign_id, with_for_update=True, populate_existing=True
        )
        plan = await self.session.get(
            PublicationPlan, plan_id, with_for_update=True, populate_existing=True
        )
        assert plan is not None
        allowed = {
            PublicationPlanStatus.DRAFT: {PublicationPlanStatus.REJECTED},
            PublicationPlanStatus.WAITING_APPROVAL: {
                PublicationPlanStatus.DRAFT,
                PublicationPlanStatus.APPROVED,
            },
            PublicationPlanStatus.APPROVED: {PublicationPlanStatus.WAITING_APPROVAL},
            PublicationPlanStatus.REJECTED: {PublicationPlanStatus.WAITING_APPROVAL},
        }
        if plan.status is target:
            return await self.get_plan(plan_id)
        if target not in allowed.get(plan.status, set()):
            raise AppError(
                "PUBLICATION_PLAN_INVALID_TRANSITION", "Недопустимый переход статуса плана.", 409
            )
        if target is PublicationPlanStatus.APPROVED:
            await self._validate_plan_items(plan)
        plan.status = target
        if target is PublicationPlanStatus.APPROVED:
            plan.approved_at = datetime.now(UTC)
            plan.approved_by_user_id = user.id
        event = {
            PublicationPlanStatus.DRAFT: "PUBLICATION_PLAN_REVISION_REQUESTED",
            PublicationPlanStatus.WAITING_APPROVAL: "PUBLICATION_PLAN_SUBMITTED",
            PublicationPlanStatus.APPROVED: "PUBLICATION_PLAN_APPROVED",
            PublicationPlanStatus.REJECTED: "PUBLICATION_PLAN_REJECTED",
        }[target]
        await ActivityLogService(self.session).record(
            event,
            operation_key=f"publication-plan:{plan.id}:{target}",
            campaign_id=plan.campaign_id,
            user_id=user.id,
            metadata={"plan_id": str(plan.id)},
        )
        await self.session.commit()
        return await self.get_plan(plan_id)
