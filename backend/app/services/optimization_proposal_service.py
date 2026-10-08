from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.campaign import Campaign, CampaignStatus
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionStatus,
    OptimizationActionType,
    OptimizationProposalStatus,
    OptimizationTargetEntityType,
)
from app.models.user import User
from app.schemas.feedback import FeedbackAnalystResult, OptimizationActionDraft
from app.schemas.optimization import OptimizationActionApplyRequest, OptimizationActionApplyResponse
from app.services.activity_log_service import ActivityLogService


def validate_action(snapshot: dict[str, Any], action: OptimizationActionDraft) -> None:
    def invalid() -> None:
        raise AppError(
            "OPTIMIZATION_ACTION_INVALID",
            "Действие не соответствует зафиксированному контексту анализа.",
            422,
        )

    evidence = {
        "publication": snapshot.get("publication_ids", []),
        "content_version": snapshot.get("content_version_ids", []),
        "metrics_snapshot": snapshot.get("metrics_snapshot_ids", []),
        "marketing_feedback": snapshot.get("feedback_ids", []),
    }
    if any(str(ref.id) not in evidence[ref.type] for ref in action.evidence_refs):
        invalid()
    allowed = snapshot.get("optimization_target_allowlist", {})
    campaign = allowed.get("campaign", {})
    if campaign.get("id") != snapshot.get("campaign_id") or campaign.get(
        "strategy_version"
    ) != snapshot.get("strategy_version"):
        invalid()
    target = str(action.target_entity_id)
    if action.type is OptimizationActionType.CONTENT_REVISION:
        if (
            action.target_entity_type is not OptimizationTargetEntityType.CONTENT_ITEM
            or action.target_version_id is None
        ):
            invalid()
        if not any(
            row["content_item_id"] == target
            and row["content_version_id"] == str(action.target_version_id)
            and row["campaign_id"] == campaign["id"]
            for row in allowed.get("content", [])
        ):
            invalid()
    elif action.type is OptimizationActionType.PUBLICATION_PLAN_REVISION:
        if (
            action.target_entity_type is not OptimizationTargetEntityType.PUBLICATION_PLAN
            or action.target_version_id is not None
        ):
            invalid()
        if not any(
            row["id"] == target and row["campaign_id"] == campaign["id"]
            for row in allowed.get("publication_plans", [])
        ):
            invalid()
    else:
        expected = (
            OptimizationTargetEntityType.CAMPAIGN_STRATEGY
            if action.type is OptimizationActionType.STRATEGY_REVIEW
            else OptimizationTargetEntityType.CAMPAIGN
        )
        if (
            action.target_entity_type is not expected
            or target != campaign["id"]
            or action.target_version_id is not None
        ):
            invalid()


class OptimizationProposalService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def materialize(
        self, analysis: MarketingFeedbackAnalysis, user: User
    ) -> CampaignOptimizationProposal | None:
        """Called with the source analysis locked; caller commits the whole acceptance."""
        if analysis.status is not FeedbackAnalysisStatus.ACCEPTED:
            raise AppError("OPTIMIZATION_ANALYSIS_NOT_ACCEPTED", "Анализ не принят.", 409)
        existing = await self.session.scalar(
            select(CampaignOptimizationProposal).where(
                CampaignOptimizationProposal.feedback_analysis_id == analysis.id
            )
        )
        if existing:
            return existing
        recommendations = analysis.recommendations
        # Legacy JSON is read as stored. Never infer targets or manufacture actions.
        if not recommendations or all("proposed_action" not in row for row in recommendations):
            return None
        if any("proposed_action" not in row for row in recommendations):
            raise AppError(
                "OPTIMIZATION_ACTION_INVALID", "Неполный типизированный результат анализа.", 422
            )
        if (
            analysis.input_snapshot.get("campaign_id") != str(analysis.campaign_id)
            or analysis.input_snapshot.get("strategy_version") != analysis.strategy_version
        ):
            raise AppError(
                "OPTIMIZATION_ACTION_INVALID", "Контекст анализа не соответствует кампании.", 422
            )
        try:
            result = FeedbackAnalystResult.model_validate(
                {
                    "summary": analysis.summary,
                    "findings": analysis.findings,
                    "recommendations": recommendations,
                    "experiment_ideas": analysis.experiment_ideas,
                    "limitations": analysis.limitations,
                },
                context={"legacy_experiment": True},
            )
            drafts = [row.proposed_action for row in result.recommendations]
            evidence = {
                "publication": analysis.input_snapshot.get("publication_ids", []),
                "content_version": analysis.input_snapshot.get("content_version_ids", []),
                "metrics_snapshot": analysis.input_snapshot.get("metrics_snapshot_ids", []),
                "marketing_feedback": analysis.input_snapshot.get("feedback_ids", []),
            }
            refs = [ref for row in result.findings for ref in row.evidence_refs] + [
                ref
                for recommendation in result.recommendations
                for ref in recommendation.evidence_refs
            ]
            if any(str(ref.id) not in evidence[ref.type] for ref in refs):
                raise AppError(
                    "FEEDBACK_EVIDENCE_INVALID", "Ссылка вне зафиксированных доказательств.", 422
                )
        except ValidationError as exc:
            raise AppError(
                "OPTIMIZATION_ACTION_INVALID", "Некорректное типизированное действие.", 422
            ) from exc
        for draft in drafts:
            validate_action(analysis.input_snapshot, draft)
        proposal = CampaignOptimizationProposal(
            campaign_id=analysis.campaign_id,
            feedback_analysis_id=analysis.id,
            strategy_version=analysis.strategy_version,
            summary=analysis.summary,
            created_by_agent_run_id=analysis.agent_run_id,
            status=OptimizationProposalStatus.WAITING_APPROVAL,
            actions=[],
        )
        self.session.add(proposal)
        await self.session.flush()
        proposal.actions = [
            CampaignOptimizationAction(
                proposal_id=proposal.id,
                position=index,
                source_recommendation_index=index,
                **draft.model_dump(exclude={"evidence_refs", "experiment_spec"}),
                experiment_spec=draft.experiment_spec.model_dump(mode="json")
                if draft.experiment_spec
                else None,
                evidence_refs=[ref.model_dump(mode="json") for ref in draft.evidence_refs],
            )
            for index, draft in enumerate(drafts)
        ]
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "OPTIMIZATION_PROPOSAL_CREATED",
            operation_key=f"optimization-proposal-created:{proposal.id}",
            campaign_id=proposal.campaign_id,
            user_id=user.id,
            metadata={
                "proposal_id": str(proposal.id),
                "feedback_analysis_id": str(analysis.id),
                "status": proposal.status.value,
            },
        )
        return proposal

    async def get(self, proposal_id: UUID) -> CampaignOptimizationProposal:
        row = await self.session.get(CampaignOptimizationProposal, proposal_id)
        if row is None:
            raise AppError("OPTIMIZATION_PROPOSAL_NOT_FOUND", "Предложение не найдено.", 404)
        return row

    async def list(self, campaign_id: UUID) -> list[CampaignOptimizationProposal]:
        return list(
            await self.session.scalars(
                select(CampaignOptimizationProposal)
                .where(CampaignOptimizationProposal.campaign_id == campaign_id)
                .order_by(CampaignOptimizationProposal.created_at.desc())
            )
        )

    async def decide(
        self, action_id: UUID, user: User, decision: OptimizationActionStatus
    ) -> CampaignOptimizationAction:
        if decision not in {OptimizationActionStatus.APPROVED, OptimizationActionStatus.REJECTED}:
            raise AppError("OPTIMIZATION_DECISION_INVALID", "Недопустимое решение.", 422)
        # Serialize the whole proposal, including sibling actions and aggregation.
        source = await self.session.get(CampaignOptimizationAction, action_id)
        if source is None:
            raise AppError("OPTIMIZATION_ACTION_NOT_FOUND", "Действие не найдено.", 404)
        proposal = await self.get(source.proposal_id)
        campaign = await self.session.get(
            Campaign, proposal.campaign_id, with_for_update=True, populate_existing=True
        )
        if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        proposal = (
            await self.session.scalars(
                select(CampaignOptimizationProposal)
                .where(CampaignOptimizationProposal.id == source.proposal_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one()
        action = await self.session.get(
            CampaignOptimizationAction, action_id, with_for_update=True, populate_existing=True
        )
        assert action is not None
        if action.status is decision:
            await self.session.commit()
            return action
        if action.status is not OptimizationActionStatus.PROPOSED:
            raise AppError(
                "OPTIMIZATION_ACTION_ALREADY_REVIEWED", "По действию уже принято решение.", 409
            )
        action.status = decision
        await self.session.flush()
        statuses = set(
            await self.session.scalars(
                select(CampaignOptimizationAction.status).where(
                    CampaignOptimizationAction.proposal_id == proposal.id
                )
            )
        )
        await self.update_aggregation(proposal)
        if OptimizationActionStatus.PROPOSED not in statuses:
            proposal.reviewed_by_user_id = user.id
            proposal.reviewed_at = datetime.now(UTC)
        await ActivityLogService(self.session).record(
            f"OPTIMIZATION_ACTION_{decision.value}",
            operation_key=f"optimization-action-decision:{action.id}",
            campaign_id=proposal.campaign_id,
            user_id=user.id,
            metadata={
                "proposal_id": str(proposal.id),
                "action_id": str(action.id),
                "feedback_analysis_id": str(proposal.feedback_analysis_id),
                "action_type": action.type.value,
                "status": decision.value,
            },
        )
        await self.session.commit()
        await self.session.refresh(action)
        return action

    @staticmethod
    def aggregate(statuses: set[OptimizationActionStatus]) -> OptimizationProposalStatus:
        if statuses == {OptimizationActionStatus.PROPOSED}:
            return OptimizationProposalStatus.WAITING_APPROVAL
        if statuses == {OptimizationActionStatus.APPROVED}:
            return OptimizationProposalStatus.APPROVED
        if statuses == {OptimizationActionStatus.REJECTED}:
            return OptimizationProposalStatus.REJECTED
        return OptimizationProposalStatus.PARTIALLY_APPROVED

    async def update_aggregation(self, proposal: CampaignOptimizationProposal) -> None:
        actions = list(
            await self.session.scalars(
                select(CampaignOptimizationAction).where(
                    CampaignOptimizationAction.proposal_id == proposal.id
                )
            )
        )
        statuses = {action.status for action in actions}
        if OptimizationActionStatus.APPLIED in statuses and not any(
            action.status in {OptimizationActionStatus.PROPOSED, OptimizationActionStatus.FAILED}
            or (
                action.status is OptimizationActionStatus.APPROVED
                and action.type is not OptimizationActionType.NO_CHANGE
            )
            for action in actions
        ):
            proposal.status = OptimizationProposalStatus.APPLIED
        else:
            proposal.status = self.aggregate(statuses)

    async def apply_action(
        self, action_id: UUID, user: User, data: "OptimizationActionApplyRequest"
    ) -> "OptimizationActionApplyResponse":
        from app.services.optimization_apply_service import OptimizationApplyService

        return await OptimizationApplyService(self.session).apply(action_id, user, data)
