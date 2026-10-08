"""Batched persisted business read model shared by dashboard and campaign next step."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign, CampaignStatus
from app.models.marketing_experiment import ExperimentStatus, MarketingExperiment
from app.models.marketing_feedback import (
    FeedbackAnalysisStatus,
    MarketingFeedback,
    MarketingFeedbackAnalysis,
)
from app.models.optimization import (
    CampaignOptimizationAction,
    CampaignOptimizationProposal,
    OptimizationActionStatus,
    OptimizationActionType,
    OptimizationProposalStatus,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.task import Task, TaskStatus, TaskType
from app.schemas.optimization_workspace import OptimizationDashboard, OptimizationWorkspaceItem


@dataclass
class OptimizationState:
    review_analyses: list[MarketingFeedbackAnalysis] = field(default_factory=list)
    decision_actions: list[CampaignOptimizationAction] = field(default_factory=list)
    apply_actions: list[CampaignOptimizationAction] = field(default_factory=list)
    draft_experiments: list[MarketingExperiment] = field(default_factory=list)
    running_experiments: list[MarketingExperiment] = field(default_factory=list)
    processing_tasks: list[Task] = field(default_factory=list)
    new_feedback_count: int = 0
    new_metrics_count: int = 0
    latest_evidence_at: datetime | None = None
    superseded_legacy_analysis_ids: list[UUID] = field(default_factory=list)


def evidence_ids(snapshot: dict[str, Any], canonical: str, legacy: str) -> set[str]:
    value = snapshot.get(canonical)
    return set(value if value is not None else snapshot.get(legacy) or [])


class OptimizationWorkspaceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def states(self, campaign_ids: list[UUID]) -> dict[UUID, OptimizationState]:
        states = {cid: OptimizationState() for cid in campaign_ids}
        if not campaign_ids:
            return states
        analyses = list(
            await self.session.scalars(
                select(MarketingFeedbackAnalysis)
                .where(MarketingFeedbackAnalysis.campaign_id.in_(campaign_ids))
                .order_by(
                    MarketingFeedbackAnalysis.created_at.desc(), MarketingFeedbackAnalysis.id.desc()
                )
            )
        )
        tasks = list(
            await self.session.scalars(
                select(Task)
                .where(
                    Task.campaign_id.in_(campaign_ids),
                    Task.task_type == TaskType.ANALYZE_PERFORMANCE,
                )
                .order_by(Task.created_at.desc(), Task.id.desc())
            )
        )
        task_by_id = {t.id: t for t in tasks}
        grouped: dict[UUID, list[MarketingFeedbackAnalysis]] = defaultdict(list)
        for analysis in analyses:
            grouped[analysis.campaign_id].append(analysis)
        for cid, rows in grouped.items():
            state = states[cid]
            reviewed = [
                a
                for a in rows
                if a.status in {FeedbackAnalysisStatus.ACCEPTED, FeedbackAnalysisStatus.REJECTED}
            ]
            for a in rows:
                legacy = a.task_id is None or a.evidence_fingerprint is None
                superseded = legacy and any(x.created_at > a.created_at for x in reviewed)
                if a.status == FeedbackAnalysisStatus.DRAFT and superseded:
                    state.superseded_legacy_analysis_ids.append(a.id)
                task = task_by_id.get(a.task_id) if a.task_id is not None else None
                if (
                    a.status == FeedbackAnalysisStatus.DRAFT
                    and a.generated_at is not None
                    and not superseded
                    and (legacy or (task is not None and task.status == TaskStatus.COMPLETED))
                ):
                    state.review_analyses.append(a)
        for task in tasks:
            if task.status in {TaskStatus.READY, TaskStatus.IN_PROGRESS}:
                states[task.campaign_id].processing_tasks.append(task)
        pairs = (
            await self.session.execute(
                select(CampaignOptimizationAction, CampaignOptimizationProposal)
                .join(
                    CampaignOptimizationProposal,
                    CampaignOptimizationProposal.id == CampaignOptimizationAction.proposal_id,
                )
                .where(CampaignOptimizationProposal.campaign_id.in_(campaign_ids))
                .order_by(
                    CampaignOptimizationProposal.created_at.desc(),
                    CampaignOptimizationAction.position,
                )
            )
        ).all()
        for action, proposal in pairs:
            if proposal.status in {
                OptimizationProposalStatus.SUPERSEDED,
                OptimizationProposalStatus.REJECTED,
            }:
                continue
            state = states[proposal.campaign_id]
            if action.status == OptimizationActionStatus.PROPOSED:
                state.decision_actions.append(action)
            elif (
                action.status == OptimizationActionStatus.APPROVED
                and action.type != OptimizationActionType.NO_CHANGE
            ):
                state.apply_actions.append(action)
        experiments = await self.session.scalars(
            select(MarketingExperiment)
            .where(MarketingExperiment.campaign_id.in_(campaign_ids))
            .order_by(MarketingExperiment.created_at.desc(), MarketingExperiment.id.desc())
        )
        for experiment in experiments:
            if experiment.status == ExperimentStatus.DRAFT:
                states[experiment.campaign_id].draft_experiments.append(experiment)
            elif experiment.status == ExperimentStatus.RUNNING:
                states[experiment.campaign_id].running_experiments.append(experiment)
        feedback = await self.session.execute(
            select(
                MarketingFeedback.id, MarketingFeedback.campaign_id, MarketingFeedback.created_at
            ).where(MarketingFeedback.campaign_id.in_(campaign_ids))
        )
        for fid, cid, at in feedback:
            previous = grouped[cid][0].input_snapshot if grouped[cid] else {}
            if str(fid) not in evidence_ids(previous, "marketing_feedback_ids", "feedback_ids"):
                states[cid].new_feedback_count += 1
                states[cid].latest_evidence_at = max(
                    filter(None, [states[cid].latest_evidence_at, at])
                )
        metrics = await self.session.execute(
            select(
                PublicationMetricsSnapshot.id,
                Publication.campaign_id,
                PublicationMetricsSnapshot.publication_id,
                PublicationMetricsSnapshot.observed_at,
            )
            .join(Publication, Publication.id == PublicationMetricsSnapshot.publication_id)
            .where(
                Publication.campaign_id.in_(campaign_ids),
                Publication.status == PublicationStatus.PUBLISHED,
            )
            .order_by(
                PublicationMetricsSnapshot.observed_at.desc(),
                PublicationMetricsSnapshot.created_at.desc(),
                PublicationMetricsSnapshot.id.desc(),
            )
        )
        seen: set[UUID] = set()
        for mid, cid, pid, at in metrics:
            if pid in seen:
                continue
            seen.add(pid)
            previous = grouped[cid][0].input_snapshot if grouped[cid] else {}
            if str(mid) not in evidence_ids(
                previous, "publication_metrics_snapshot_ids", "metrics_snapshot_ids"
            ):
                states[cid].new_metrics_count += 1
                states[cid].latest_evidence_at = max(
                    filter(None, [states[cid].latest_evidence_at, at])
                )
        return states

    async def dashboard(self) -> OptimizationDashboard:
        campaigns = list(
            await self.session.scalars(
                select(Campaign)
                .where(Campaign.status != CampaignStatus.ARCHIVED)
                .order_by(Campaign.name, Campaign.id)
            )
        )
        states = await self.states([c.id for c in campaigns])
        result = OptimizationDashboard()
        for campaign in campaigns:
            state = states[campaign.id]
            base = f"/campaigns/{campaign.id}/performance"
            groups = [
                (
                    "campaigns_with_new_results",
                    state.new_feedback_count + state.new_metrics_count,
                    base,
                    state.latest_evidence_at,
                ),
                (
                    "analyses_waiting_review",
                    len(state.review_analyses),
                    base
                    + (f"#analysis-{state.review_analyses[0].id}" if state.review_analyses else ""),
                    state.review_analyses[0].generated_at if state.review_analyses else None,
                ),
                (
                    "actions_waiting_decision",
                    len(state.decision_actions),
                    base
                    + (
                        f"#proposal-{state.decision_actions[0].proposal_id}"
                        if state.decision_actions
                        else ""
                    ),
                    state.decision_actions[0].created_at if state.decision_actions else None,
                ),
                (
                    "actions_waiting_apply",
                    len(state.apply_actions),
                    base + (f"#action-{state.apply_actions[0].id}" if state.apply_actions else ""),
                    state.apply_actions[0].updated_at if state.apply_actions else None,
                ),
                (
                    "experiments_running",
                    len(state.running_experiments),
                    base
                    + (
                        f"#experiment-{state.running_experiments[0].id}"
                        if state.running_experiments
                        else ""
                    ),
                    state.running_experiments[0].updated_at if state.running_experiments else None,
                ),
            ]
            for category, count, href, at in groups:
                if count:
                    result.counts[category] += (
                        1 if category == "campaigns_with_new_results" else count
                    )
                    result.items.append(
                        OptimizationWorkspaceItem(
                            campaign_id=campaign.id,
                            campaign_name=campaign.name,
                            category=category,
                            count=count,
                            href=href,
                            latest_at=at,
                        )
                    )
        return result
