from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem, ContentStatus, ContentVersion
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionStatus,
    OptimizationActionType,
)
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.feedback import OptimizationActionDraft
from app.schemas.optimization import (
    OptimizationActionApplyRequest,
    OptimizationActionApplyResponse,
    OptimizationActionResponse,
    OptimizationAppliedArtifact,
)
from app.schemas.publication_plan import PublicationPlanGenerateRequest
from app.services.activity_log_service import ActivityLogService
from app.services.agent_run_service import AgentRunService
from app.services.campaign_planning_service import CampaignPlanningService
from app.services.content_revision_service import prepare_content_revision
from app.services.publication_plan_service import PublicationPlanService

ACTIONABLE = frozenset(
    {
        OptimizationActionType.CONTENT_REVISION,
        OptimizationActionType.PUBLICATION_PLAN_REVISION,
        OptimizationActionType.STRATEGY_REVIEW,
        OptimizationActionType.EXPERIMENT,
    }
)


class OptimizationApplyService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def artifact(
        self, action: CampaignOptimizationAction
    ) -> OptimizationAppliedArtifact | None:
        if action.status is not OptimizationActionStatus.APPLIED:
            return None
        if action.type is OptimizationActionType.EXPERIMENT:
            from app.models.marketing_experiment import MarketingExperiment

            experiment = await self.session.scalar(
                select(MarketingExperiment).where(
                    MarketingExperiment.source_optimization_action_id == action.id
                )
            )
            if experiment is not None:
                return OptimizationAppliedArtifact(
                    artifact_type="MARKETING_EXPERIMENT",
                    artifact_id=experiment.id,
                    experiment_id=experiment.id,
                    status=experiment.status.value,
                    href=f"/campaigns/{experiment.campaign_id}#experiment-{experiment.id}",
                )
        plan = await self.session.scalar(
            select(PublicationPlan).where(PublicationPlan.optimization_action_id == action.id)
        )
        task = await self.session.scalar(
            select(Task).where(Task.optimization_action_id == action.id)
        )
        if plan is not None:
            run = (
                await self.session.get(AgentRun, plan.generated_by_agent_run_id)
                if plan.generated_by_agent_run_id
                else None
            )
            return OptimizationAppliedArtifact(
                artifact_type="PUBLICATION_PLAN",
                artifact_id=plan.id,
                publication_plan_id=plan.id,
                task_id=run.task_id if run else None,
                agent_run_id=run.id if run else None,
                status="FAILED"
                if run and run.status is AgentRunStatus.FAILED
                else plan.status.value,
                error_message=run.error_message if run else None,
                href=f"/campaigns/{plan.campaign_id}#publication-plan-{plan.id}",
            )
        if task is not None:
            run = await self.session.scalar(
                select(AgentRun)
                .where(AgentRun.task_id == task.id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(1)
            )
            return OptimizationAppliedArtifact(
                artifact_type="TASK",
                artifact_id=task.id,
                task_id=task.id,
                agent_run_id=run.id if run else None,
                status=task.status.value,
                error_message=task.error_message,
                href=f"/tasks/{task.id}",
            )
        raise AppError(
            "OPTIMIZATION_ARTIFACT_MISSING",
            "Применённое действие не имеет связанного результата.",
            409,
        )

    async def action_response(
        self, action: CampaignOptimizationAction
    ) -> OptimizationActionResponse:
        response = OptimizationActionResponse.model_validate(action)
        response.applied_artifact = await self.artifact(action)
        return response

    async def response(self, action: CampaignOptimizationAction) -> OptimizationActionApplyResponse:
        artifact = await self.artifact(action)
        assert artifact is not None
        return OptimizationActionApplyResponse(
            **artifact.model_dump(), action=await self.action_response(action)
        )

    async def apply(
        self, action_id: UUID, user: User, data: OptimizationActionApplyRequest
    ) -> OptimizationActionApplyResponse:
        from app.services.optimization_proposal_service import (
            OptimizationProposalService,
            validate_action,
        )

        source = await self.session.get(CampaignOptimizationAction, action_id)
        if source is None:
            raise AppError("OPTIMIZATION_ACTION_NOT_FOUND", "Действие не найдено.", 404)
        parent = await self.session.get(CampaignOptimizationProposal, source.proposal_id)
        assert parent is not None
        # The same stable lock order as human decision; refresh stale identity-map values.
        campaign = await self.session.get(
            Campaign, parent.campaign_id, with_for_update=True, populate_existing=True
        )
        if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        proposal = await self.session.get(
            CampaignOptimizationProposal,
            source.proposal_id,
            with_for_update=True,
            populate_existing=True,
        )
        action = await self.session.get(
            CampaignOptimizationAction, action_id, with_for_update=True, populate_existing=True
        )
        assert proposal is not None and action is not None
        if action.type is not OptimizationActionType.EXPERIMENT and data.experiment is not None:
            raise AppError(
                "EXPERIMENT_CONFIG_NOT_APPLICABLE",
                "Параметры эксперимента недопустимы для этого действия.",
                422,
            )
        if action.status is OptimizationActionStatus.APPLIED:
            result = await self.response(action)
            await self.session.commit()
            return result
        if action.status is OptimizationActionStatus.REJECTED:
            raise AppError("OPTIMIZATION_ACTION_REJECTED", "Действие отклонено.", 409)
        if action.status is OptimizationActionStatus.FAILED:
            raise AppError(
                "OPTIMIZATION_ACTION_FAILED",
                "Действие с ошибкой требует отдельного восстановления.",
                409,
            )
        if action.status is not OptimizationActionStatus.APPROVED:
            raise AppError("OPTIMIZATION_ACTION_NOT_APPROVED", "Сначала примите рекомендацию.", 409)
        if action.type not in ACTIONABLE:
            message = (
                "Workflow эксперимента пока не реализован."
                if action.type is OptimizationActionType.EXPERIMENT
                else "Изменения не требуются."
            )
            raise AppError("OPTIMIZATION_ACTION_NOT_APPLICABLE", message, 409)
        if proposal.strategy_version != campaign.strategy_version:
            raise AppError(
                "OPTIMIZATION_CONTEXT_STALE", "Версия стратегии изменилась после анализа.", 409
            )
        analysis = await self.session.get(MarketingFeedbackAnalysis, proposal.feedback_analysis_id)
        if (
            analysis is None
            or analysis.status is not FeedbackAnalysisStatus.ACCEPTED
            or analysis.campaign_id != campaign.id
            or analysis.strategy_version != proposal.strategy_version
        ):
            raise AppError(
                "OPTIMIZATION_CONTEXT_STALE", "Исходный принятый анализ недоступен.", 409
            )
        if action.type is OptimizationActionType.EXPERIMENT:
            if action.experiment_spec is None:
                from app.services.experiment_service import LEGACY_SPEC_MESSAGE

                raise AppError("OPTIMIZATION_EXPERIMENT_SPEC_MISSING", LEGACY_SPEC_MESSAGE, 409)
            if data.experiment is None:
                raise AppError(
                    "EXPERIMENT_CONFIG_REQUIRED", "Выберите периоды и публикации эксперимента.", 422
                )
        draft = OptimizationActionDraft.model_validate(
            {
                key: getattr(action, key)
                for key in (
                    "type",
                    "experiment_spec",
                    "target_entity_type",
                    "target_entity_id",
                    "target_version_id",
                    "reason",
                    "expected_effect",
                    "priority",
                    "evidence_refs",
                )
            }
        )
        validate_action(analysis.input_snapshot, draft)
        if action.type is OptimizationActionType.EXPERIMENT:
            from app.services.experiment_service import ExperimentService

            assert data.experiment is not None
            return await ExperimentService(self.session).apply_locked(
                campaign, proposal, action, user, data.experiment
            )
        action_snapshot = {
            "id": str(action.id),
            "status": action.status.value,
            **draft.model_dump(mode="json"),
        }
        from app.services.feedback_service import FeedbackService

        accepted = await FeedbackService(self.session).accepted_snapshot(analysis.id, campaign.id)
        accepted = {**accepted, "summary": analysis.summary, "status": analysis.status.value}
        context: dict[str, Any] = {
            "optimization_action_id": str(action.id),
            "optimization_proposal_id": str(proposal.id),
            "accepted_analysis_id": str(analysis.id),
            "feedback_analysis_id": str(analysis.id),
            "accepted_feedback_analysis": accepted,
            "approved_optimization_action": action_snapshot,
            "human_comment": data.human_comment,
        }
        run: AgentRun | None = None
        plan: PublicationPlan | None = None
        if action.type is OptimizationActionType.CONTENT_REVISION:
            if not data.human_comment or not data.human_comment.strip():
                raise AppError(
                    "OPTIMIZATION_HUMAN_COMMENT_REQUIRED",
                    "Для доработки нужен комментарий человека.",
                    422,
                )
            item = await self.session.get(
                ContentItem, action.target_entity_id, with_for_update=True, populate_existing=True
            )
            version = await self.session.get(ContentVersion, action.target_version_id)
            if (
                item is None
                or item.campaign_id != campaign.id
                or version is None
                or version.content_item_id != item.id
                or item.current_version_id != action.target_version_id
                or item.status is not ContentStatus.APPROVED
            ):
                raise AppError(
                    "OPTIMIZATION_TARGET_STALE",
                    "Точная исходная версия контента больше не актуальна.",
                    409,
                )
            task = await prepare_content_revision(
                self.session,
                item.id,
                user,
                data.human_comment,
                optimization_action_id=action.id,
                provenance={
                    **context,
                    "source_content_item_id": str(item.id),
                    "source_content_version_id": str(version.id),
                },
            )
            artifact_type, artifact_id = "TASK", task.id
        elif action.type is OptimizationActionType.PUBLICATION_PLAN_REVISION:
            source_plan = await self.session.scalar(
                select(PublicationPlan)
                .where(PublicationPlan.id == action.target_entity_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            latest_id = await self.session.scalar(
                select(PublicationPlan.id)
                .where(
                    PublicationPlan.campaign_id == campaign.id,
                    PublicationPlan.status == PublicationPlanStatus.APPROVED,
                )
                .order_by(
                    PublicationPlan.approved_at.desc().nullslast(),
                    PublicationPlan.created_at.desc(),
                    PublicationPlan.id.desc(),
                )
                .limit(1)
            )
            if (
                source_plan is None
                or source_plan.campaign_id != campaign.id
                or source_plan.status is not PublicationPlanStatus.APPROVED
                or latest_id != source_plan.id
            ):
                raise AppError(
                    "OPTIMIZATION_TARGET_STALE",
                    "Утверждённый медиаплан заменён или недоступен.",
                    409,
                )
            items = list(
                await self.session.scalars(
                    select(PublicationPlanItem)
                    .where(
                        PublicationPlanItem.publication_plan_id == source_plan.id,
                        PublicationPlanItem.status == PublicationPlanItemStatus.PLANNED,
                    )
                    .order_by(PublicationPlanItem.position)
                )
            )
            if not items:
                raise AppError(
                    "OPTIMIZATION_TARGET_STALE", "В исходном плане нет активных пунктов.", 409
                )
            source_context = {
                "id": str(source_plan.id),
                "status": source_plan.status.value,
                "planning_horizon_start": source_plan.planning_horizon_start.isoformat(),
                "planning_horizon_end": source_plan.planning_horizon_end.isoformat(),
                "timezone_policy": source_plan.timezone_policy,
                "items": [
                    {
                        "source_claim_ids": item.source_claim_ids or [],
                        "source_support_summary": item.source_support_summary,
                        **{
                            key: str(getattr(item, key))
                            for key in (
                                "id",
                                "channel",
                                "scheduled_at",
                                "source_content_item_id",
                                "source_content_version_id",
                                "topic",
                                "angle",
                                "purpose",
                                "format",
                                "message_brief",
                            )
                        },
                    }
                    for item in items
                ],
            }
            plan = await PublicationPlanService(self.session).queue_generation(
                campaign.id,
                user,
                PublicationPlanGenerateRequest(
                    planning_horizon_start=source_plan.planning_horizon_start,
                    planning_horizon_end=source_plan.planning_horizon_end,
                    timezone_policy=source_plan.timezone_policy,
                    channels=sorted({item.channel for item in items}),
                    total_items=len(items),
                    feedback_analysis_id=analysis.id,
                ),
                commit=False,
                optimization_action_id=action.id,
                optimization_proposal_id=proposal.id,
                optimization_context={**context, "source_publication_plan": source_context},
            )
            run = await self.session.get(AgentRun, plan.generated_by_agent_run_id)
            assert run is not None
            plan_task = await self.session.get(Task, run.task_id)
            assert plan_task is not None
            task = plan_task
            artifact_type, artifact_id = "PUBLICATION_PLAN", plan.id
        else:
            task, run = await CampaignPlanningService(self.session).prepare_optimization_revision(
                campaign, user, action.id, context
            )
            artifact_type, artifact_id = "TASK", task.id
        # Flush relational provenance before APPLIED, inside the same durable transaction.
        await self.session.flush()
        action.status = OptimizationActionStatus.APPLIED
        action.applied_by_user_id = user.id
        action.applied_at = datetime.now(UTC)
        await self.session.flush()
        await OptimizationProposalService(self.session).update_aggregation(proposal)
        metadata = {
            "proposal_id": str(proposal.id),
            "action_id": str(action.id),
            "feedback_analysis_id": str(analysis.id),
            "action_type": action.type.value,
            "artifact_type": artifact_type,
            "artifact_id": str(artifact_id),
            "task_id": str(task.id),
        }
        if plan is not None:
            metadata["publication_plan_id"] = str(plan.id)
        if run is not None:
            metadata["agent_run_id"] = str(run.id)
        await ActivityLogService(self.session).record(
            "OPTIMIZATION_ACTION_APPLIED",
            operation_key=f"optimization-action-applied:{action.id}",
            campaign_id=campaign.id,
            task_id=task.id,
            user_id=user.id,
            metadata=metadata,
        )
        await self.session.commit()
        try:
            if plan is not None:
                assert run is not None
                await PublicationPlanService(self.session).enqueue_generation(plan, run)
            else:
                service = AgentRunService(self.session)
                if run is None:
                    run = await service.create_queued_run(task.id)
                await service.enqueue(run)
        except AppError as exc:
            # The artifact is already durable. Expose failure, never rerun Apply/duplicate it.
            current = await self.session.get(Task, task.id)
            assert current is not None
            current.status = TaskStatus.FAILED
            current.error_message = exc.message
            await self.session.commit()
        await self.session.refresh(action)
        return await self.response(action)
