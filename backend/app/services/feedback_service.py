from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.models.agent import AgentStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.campaign_kpi import CampaignKPI
from app.models.content import ContentItem, ContentVersion
from app.models.marketing_feedback import (
    AnalysisTriggerSource,
    FeedbackAnalysisStatus,
    FeedbackSource,
    MarketingFeedback,
    MarketingFeedbackAnalysis,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.publication_plan import PublicationPlan, PublicationPlanItem
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.agents import AgentRepository
from app.schemas.feedback import FeedbackAnalystResult
from app.services.activity_log_service import ActivityLogService
from app.services.campaign_performance_service import RAW_FIELDS
from app.services.optimization_proposal_service import OptimizationProposalService, validate_action
from app.services.performance_evidence import evidence_fingerprint


class FeedbackService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.prepared_new = False

    async def create_feedback(
        self, campaign_id: UUID, user: User, data: dict[str, Any]
    ) -> MarketingFeedback:
        publication_id = data.get("publication_id")
        content_item_id = data.get("content_item_id")
        content_version_id = data.get("content_version_id")
        if publication_id:
            publication = await self.session.get(Publication, publication_id)
            if publication is None or publication.campaign_id != campaign_id:
                raise AppError(
                    "FEEDBACK_REFERENCE_INVALID",
                    "Публикация не принадлежит кампании.",
                    422,
                )
            content_item_id = content_item_id or publication.content_item_id
            content_version_id = content_version_id or publication.content_version_id
        if content_item_id:
            item = await self.session.get(ContentItem, content_item_id)
            if item is None or item.campaign_id != campaign_id:
                raise AppError(
                    "FEEDBACK_REFERENCE_INVALID",
                    "Контент не принадлежит кампании.",
                    422,
                )
        if content_version_id:
            version = await self.session.get(ContentVersion, content_version_id)
            if version is None:
                raise AppError("FEEDBACK_REFERENCE_INVALID", "Версия контента не найдена.", 422)
            if content_item_id and version.content_item_id != content_item_id:
                raise AppError("FEEDBACK_REFERENCE_INVALID", "Версия не принадлежит контенту.", 422)
            if not content_item_id:
                item = await self.session.get(ContentItem, version.content_item_id)
                if item is None or item.campaign_id != campaign_id:
                    raise AppError(
                        "FEEDBACK_REFERENCE_INVALID",
                        "Версия не принадлежит кампании.",
                        422,
                    )
                content_item_id = version.content_item_id
        row = MarketingFeedback(
            campaign_id=campaign_id,
            publication_id=publication_id,
            content_item_id=content_item_id,
            content_version_id=content_version_id,
            source_type=FeedbackSource.HUMAN,
            category=data["category"],
            rating=data.get("rating"),
            comment=data["comment"],
            observed_at=datetime.now(UTC),
            created_by_user_id=user.id,
        )
        self.session.add(row)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "MARKETING_FEEDBACK_CREATED",
            operation_key=f"feedback-created:{row.id}",
            campaign_id=campaign_id,
            content_item_id=content_item_id,
            user_id=user.id,
            metadata={"feedback_id": str(row.id), "category": str(row.category)},
        )
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def list_feedback(self, campaign_id: UUID) -> list[MarketingFeedback]:
        return list(
            (
                await self.session.scalars(
                    select(MarketingFeedback)
                    .where(MarketingFeedback.campaign_id == campaign_id)
                    .order_by(MarketingFeedback.id)
                )
            ).all()
        )

    async def _input_snapshot(self, campaign_id: UUID) -> dict[str, Any]:
        feedback = await self.list_feedback(campaign_id)
        publications = list(
            (
                await self.session.scalars(
                    select(Publication)
                    .where(
                        Publication.campaign_id == campaign_id,
                        Publication.status == PublicationStatus.PUBLISHED,
                    )
                    .order_by(Publication.id)
                )
            ).all()
        )
        publication_ids = [row.id for row in publications]
        snapshots: list[PublicationMetricsSnapshot] = []
        for publication_id in publication_ids:
            latest = await self.session.scalar(
                select(PublicationMetricsSnapshot)
                .where(PublicationMetricsSnapshot.publication_id == publication_id)
                .order_by(
                    PublicationMetricsSnapshot.observed_at.desc(),
                    PublicationMetricsSnapshot.created_at.desc(),
                    PublicationMetricsSnapshot.id.desc(),
                )
                .limit(1)
            )
            if latest:
                snapshots.append(latest)
        campaign = await self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        versions = list(
            await self.session.scalars(
                select(ContentVersion)
                .join(ContentItem, ContentVersion.content_item_id == ContentItem.id)
                .where(
                    ContentItem.campaign_id == campaign_id,
                    ContentVersion.id.in_([row.content_version_id for row in publications]),
                )
                .order_by(ContentVersion.id)
            )
        )
        plans = list(
            await self.session.scalars(
                select(PublicationPlan)
                .where(PublicationPlan.campaign_id == campaign_id)
                .order_by(PublicationPlan.id)
            )
        )
        plan_context = []
        for plan in plans:
            items = list(
                await self.session.scalars(
                    select(PublicationPlanItem)
                    .where(PublicationPlanItem.publication_plan_id == plan.id)
                    .order_by(PublicationPlanItem.position, PublicationPlanItem.id)
                )
            )
            plan_context.append(
                {
                    "id": str(plan.id),
                    "campaign_id": str(campaign_id),
                    "status": plan.status.value,
                    "updated_at": plan.updated_at.isoformat(),
                    "planning_horizon_start": plan.planning_horizon_start.isoformat(),
                    "planning_horizon_end": plan.planning_horizon_end.isoformat(),
                    "items": [
                        {
                            "id": str(item.id),
                            "position": item.position,
                            "status": item.status.value,
                            "source_content_item_id": str(item.source_content_item_id),
                            "source_content_version_id": str(item.source_content_version_id),
                            "updated_at": item.updated_at.isoformat(),
                        }
                        for item in items
                    ],
                }
            )
        kpis = list(
            await self.session.scalars(
                select(CampaignKPI)
                .where(CampaignKPI.campaign_id == campaign_id)
                .order_by(CampaignKPI.id)
            )
        )
        timestamps = [row.observed_at for row in snapshots] + [
            row.observed_at for row in feedback if row.observed_at is not None
        ]
        start = min(timestamps) if timestamps else None
        end = max(timestamps) if timestamps else None
        if start is not None and start == end:
            end = start + timedelta(microseconds=1)
        count = len(publications)
        return {
            "campaign": {
                "id": str(campaign.id),
                "name": campaign.name,
                "goal": campaign.goal,
                "strategy": campaign.strategy,
            },
            "publication_metrics_snapshot_ids": sorted(str(row.id) for row in snapshots),
            "marketing_feedback_ids": sorted(str(row.id) for row in feedback),
            "campaign_kpi_ids": [str(row.id) for row in kpis],
            "campaign_kpis": [
                {
                    "id": str(row.id),
                    "metric": row.metric.value,
                    "channel": row.channel.value if row.channel else None,
                    "target_value": str(row.target_value),
                    "comparison": row.comparison.value,
                    "period_start": row.period_start.isoformat(),
                    "period_end": row.period_end.isoformat(),
                    "is_active": row.is_active,
                }
                for row in kpis
            ],
            "analysis_period_start": start.isoformat() if start else None,
            "analysis_period_end": end.isoformat() if end else None,
            "publication_plan_context": plan_context,
            "data_quality": {
                "published_publication_count": count,
                "publications_with_metrics": len(snapshots),
                "publications_without_metrics": count - len(snapshots),
                "raw_metric_coverage": {
                    field: sum(getattr(row, field) is not None for row in snapshots) / count
                    if count
                    else 0
                    for field in RAW_FIELDS
                },
                "human_feedback_count": len(feedback),
                "newest_snapshot_observed_at": max(row.observed_at for row in snapshots).isoformat()
                if snapshots
                else None,
            },
            "publications": [
                {
                    "publication_id": str(row.id),
                    "content_version_id": str(row.content_version_id),
                    "channel": row.channel.value,
                    "published_at": row.published_at.isoformat() if row.published_at else None,
                }
                for row in publications
            ],
            "metrics": [
                {
                    "id": str(row.id),
                    "publication_id": str(row.publication_id),
                    "observed_at": row.observed_at.isoformat(),
                    "source": row.source.value,
                    "provider": row.provider,
                    **{field: getattr(row, field) for field in RAW_FIELDS},
                }
                for row in snapshots
            ],
            "feedback": [
                {
                    "id": str(row.id),
                    "publication_id": str(row.publication_id) if row.publication_id else None,
                    "category": row.category.value,
                    "rating": row.rating,
                    "comment": row.comment,
                    "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                }
                for row in feedback
            ],
            "optimization_target_allowlist": {
                "campaign": {"id": str(campaign_id), "strategy_version": campaign.strategy_version},
                "content": [
                    {
                        "campaign_id": str(campaign_id),
                        "content_item_id": str(version.content_item_id),
                        "content_version_id": str(version.id),
                    }
                    for version in versions
                ],
                "publication_plans": plan_context,
            },
            "campaign_id": str(campaign_id),
            "strategy_version": campaign.strategy_version,
            "strategy_reference": {
                "campaign_id": str(campaign_id),
                "strategy_version": campaign.strategy_version,
            },
            "publication_ids": [str(row.id) for row in publications],
            "content_version_ids": sorted({str(row.content_version_id) for row in publications}),
            "metrics_snapshot_ids": [str(row.id) for row in snapshots],
            "metric_sources": sorted({str(row.source) for row in snapshots}),
            "feedback_ids": [str(row.id) for row in feedback],
            "publications_analyzed": len(publications),
            "feedback_count": len(feedback),
            "metric_coverage_ratio": (len(snapshots) / len(publications) if publications else 0.0),
        }

    async def prepare_analysis(
        self,
        campaign_id: UUID,
        trigger_source: AnalysisTriggerSource = AnalysisTriggerSource.MANUAL,
        requested_by_user_id: UUID | None = None,
        *,
        automatic: bool = False,
    ) -> MarketingFeedbackAnalysis | None:
        self.prepared_new = False
        campaign = await self.session.scalar(
            select(Campaign)
            .where(Campaign.id == campaign_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        if campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        snapshot = await self._input_snapshot(campaign_id)
        fingerprint = evidence_fingerprint(snapshot)
        existing = await self.session.scalar(
            select(MarketingFeedbackAnalysis).where(
                MarketingFeedbackAnalysis.campaign_id == campaign_id,
                MarketingFeedbackAnalysis.evidence_fingerprint == fingerprint,
            )
        )
        if existing is not None:
            await self.session.commit()
            return existing
        if automatic:
            latest = await self.session.scalar(
                select(MarketingFeedbackAnalysis)
                .where(MarketingFeedbackAnalysis.campaign_id == campaign_id)
                .order_by(
                    MarketingFeedbackAnalysis.created_at.desc(), MarketingFeedbackAnalysis.id.desc()
                )
                .limit(1)
            )
            active = await self.session.scalar(
                select(MarketingFeedbackAnalysis.id)
                .where(
                    MarketingFeedbackAnalysis.campaign_id == campaign_id,
                    MarketingFeedbackAnalysis.status == FeedbackAnalysisStatus.DRAFT,
                    MarketingFeedbackAnalysis.task_id.is_not(None),
                    MarketingFeedbackAnalysis.evidence_fingerprint.is_not(None),
                )
                .limit(1)
            )
            if (
                not (snapshot["metrics_snapshot_ids"] or snapshot["feedback_ids"])
                or active is not None
                or (
                    latest is not None
                    and latest.created_at
                    > datetime.now(UTC)
                    - timedelta(hours=settings.optimization_analysis_cooldown_hours)
                )
            ):
                await self.session.commit()
                return None
        agent = await AgentRepository(self.session).get_by_slug("marketing_analyst")
        if agent is None or agent.status is not AgentStatus.ACTIVE:
            raise AppError(
                "MARKETING_ANALYST_UNAVAILABLE", "Marketing Analyst отсутствует или неактивен.", 409
            )
        task = Task(
            campaign_id=campaign_id,
            task_type=TaskType.ANALYZE_PERFORMANCE,
            title="Анализ результатов кампании",
            assigned_agent_id=agent.id,
            status=TaskStatus.READY,
            input_data={"evidence_fingerprint": fingerprint},
        )
        self.session.add(task)
        await self.session.flush()
        row = MarketingFeedbackAnalysis(
            campaign_id=campaign_id,
            task_id=task.id,
            evidence_fingerprint=fingerprint,
            trigger_source=trigger_source,
            status=FeedbackAnalysisStatus.DRAFT,
            strategy_version=campaign.strategy_version,
            input_snapshot=snapshot,
            summary="",
            findings=[],
            interpretations=[],
            recommendations=[],
            experiment_ideas=[],
            limitations=[],
        )
        self.session.add(row)
        await self.session.flush()
        task.input_data = {**task.input_data, "analysis_id": str(row.id)}
        self.prepared_new = True
        await self.analysis_event(
            row, "PERFORMANCE_ANALYSIS_DISCOVERED", user_id=requested_by_user_id
        )
        await self.session.commit()
        return row

    async def analysis_event(
        self,
        row: MarketingFeedbackAnalysis,
        event: str,
        *,
        user_id: UUID | None = None,
        suffix: str = "",
    ) -> None:
        await ActivityLogService(self.session).record(
            event,
            operation_key=f"{event}:{row.id}:{row.agent_run_id}:{suffix}",
            campaign_id=row.campaign_id,
            task_id=row.task_id,
            user_id=user_id,
            metadata={
                "campaign_id": str(row.campaign_id),
                "analysis_id": str(row.id),
                "task_id": str(row.task_id) if row.task_id else None,
                "agent_run_id": str(row.agent_run_id) if row.agent_run_id else None,
                "evidence_fingerprint": row.evidence_fingerprint,
                "trigger_source": row.trigger_source.value if row.trigger_source else None,
            },
        )

    async def queue_analysis(self, campaign_id: UUID) -> MarketingFeedbackAnalysis:
        row = await self.prepare_analysis(campaign_id)
        assert row is not None
        return row

    async def retry_analysis(self, analysis_id: UUID) -> MarketingFeedbackAnalysis:
        # Campaign first, matching preparation/review lock ordering.
        original = await self.session.get(MarketingFeedbackAnalysis, analysis_id)
        if original is None:
            raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
        campaign = await self.session.get(Campaign, original.campaign_id, with_for_update=True)
        if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        row = await self.session.scalar(
            select(MarketingFeedbackAnalysis)
            .where(MarketingFeedbackAnalysis.id == analysis_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert row is not None
        task = (
            await self.session.get(Task, row.task_id, with_for_update=True) if row.task_id else None
        )
        if task is None or task.task_type is not TaskType.ANALYZE_PERFORMANCE:
            raise AppError("ANALYSIS_TASK_NOT_FOUND", "Задача анализа не найдена.", 409)
        if row.status is FeedbackAnalysisStatus.DRAFT and (
            task.status is TaskStatus.READY or task.retry_count > 0
        ):
            await self.session.commit()
            return row
        if row.status is not FeedbackAnalysisStatus.FAILED:
            raise AppError("ANALYSIS_NOT_FAILED", "Повторить можно только анализ с ошибкой.", 409)
        row.status = FeedbackAnalysisStatus.DRAFT
        task.status = TaskStatus.READY
        task.started_at = task.completed_at = None
        task.error_message = None
        task.output_data = {}
        await self.analysis_event(
            row, "FEEDBACK_ANALYSIS_RETRY_REQUESTED", suffix=str(task.retry_count)
        )
        task.retry_count += 1
        await self.session.commit()
        return row

    @staticmethod
    def validate_evidence(snapshot: dict[str, Any], result: FeedbackAnalystResult) -> None:
        allowed = {
            "publication": set(snapshot["publication_ids"]),
            "content_version": set(snapshot["content_version_ids"]),
            "metrics_snapshot": set(snapshot["metrics_snapshot_ids"]),
            "marketing_feedback": set(snapshot["feedback_ids"]),
        }
        for recommendation in result.recommendations:
            validate_action(snapshot, recommendation.proposed_action)
        for item in [*result.findings, *result.recommendations]:
            for reference in getattr(item, "evidence_refs", []):
                if str(reference.id) not in allowed[reference.type]:
                    raise AppError(
                        "FEEDBACK_EVIDENCE_INVALID",
                        "Анализ содержит ссылку вне зафиксированного набора доказательств.",
                        422,
                    )

    async def complete_with_result(
        self, analysis_id: UUID, run_id: UUID, result: FeedbackAnalystResult
    ) -> MarketingFeedbackAnalysis:
        row = await self.session.get(MarketingFeedbackAnalysis, analysis_id, with_for_update=True)
        if row is None or row.agent_run_id != run_id:
            raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
        result = FeedbackAnalystResult.model_validate(result.model_dump(mode="json"))
        self.validate_evidence(row.input_snapshot, result)
        row.summary = result.summary
        row.interpretations = [item.model_dump(mode="json") for item in result.interpretations]
        row.findings = [item.model_dump(mode="json") for item in result.findings]
        row.recommendations = [item.model_dump(mode="json") for item in result.recommendations]
        row.experiment_ideas = [item.model_dump(mode="json") for item in result.experiment_ideas]
        row.limitations = result.limitations
        row.generated_at = datetime.now(UTC)
        row.status = FeedbackAnalysisStatus.DRAFT
        await self.analysis_event(row, "FEEDBACK_ANALYSIS_COMPLETED")
        return row

    async def accepted_snapshot(self, analysis_id: UUID, campaign_id: UUID) -> dict[str, Any]:
        analysis = await self.session.get(MarketingFeedbackAnalysis, analysis_id)
        if analysis is None or analysis.campaign_id != campaign_id:
            raise AppError("FEEDBACK_ANALYSIS_INVALID", "Анализ не принадлежит кампании.", 422)
        if analysis.status is not FeedbackAnalysisStatus.ACCEPTED:
            raise AppError(
                "FEEDBACK_ANALYSIS_NOT_ACCEPTED",
                "Можно использовать только принятый анализ обратной связи.",
                409,
            )
        return {
            "analysis_id": str(analysis.id),
            "evidence_fingerprint": analysis.evidence_fingerprint,
            "interpretations": analysis.interpretations,
            "campaign_kpis": analysis.input_snapshot.get("campaign_kpis", []),
            "analysis_period_start": analysis.input_snapshot.get("analysis_period_start"),
            "analysis_period_end": analysis.input_snapshot.get("analysis_period_end"),
            "data_quality": analysis.input_snapshot.get("data_quality", {}),
            "strategy_version": analysis.strategy_version,
            "input_snapshot": analysis.input_snapshot,
            "findings": analysis.findings,
            "recommendations": analysis.recommendations,
            "experiment_ideas": analysis.experiment_ideas,
            "limitations": analysis.limitations,
        }

    async def list_analyses(self, campaign_id: UUID) -> list[MarketingFeedbackAnalysis]:
        return list(
            (
                await self.session.scalars(
                    select(MarketingFeedbackAnalysis)
                    .where(MarketingFeedbackAnalysis.campaign_id == campaign_id)
                    .order_by(MarketingFeedbackAnalysis.created_at.desc())
                )
            ).all()
        )

    async def review(
        self, analysis_id: UUID, user: User, status: FeedbackAnalysisStatus
    ) -> MarketingFeedbackAnalysis:
        original = await self.session.get(MarketingFeedbackAnalysis, analysis_id)
        if original is None:
            raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
        campaign = await self.session.get(Campaign, original.campaign_id, with_for_update=True)
        if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
            raise AppError(
                "CAMPAIGN_ARCHIVED", "Архивная кампания доступна только для чтения.", 409
            )
        row = await self.session.get(
            MarketingFeedbackAnalysis, analysis_id, with_for_update=True, populate_existing=True
        )
        if row is None:
            raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
        if row.status is not FeedbackAnalysisStatus.DRAFT:
            if row.status is status:
                return row
            raise AppError("FEEDBACK_ANALYSIS_ALREADY_REVIEWED", "Анализ уже рассмотрен.", 409)
        if row.task_id is not None and row.generated_at is None:
            raise AppError("ANALYSIS_NOT_GENERATED", "Анализ ещё не завершён.", 409)
        row.status = status
        if status is FeedbackAnalysisStatus.ACCEPTED:
            await OptimizationProposalService(self.session).materialize(row, user)
        row.reviewed_by_user_id = user.id
        row.reviewed_at = datetime.now(UTC)
        event = (
            "FEEDBACK_ANALYSIS_ACCEPTED"
            if status is FeedbackAnalysisStatus.ACCEPTED
            else "FEEDBACK_ANALYSIS_REJECTED"
        )
        await ActivityLogService(self.session).record(
            event,
            operation_key=f"feedback-analysis-review:{row.id}:{status.value}",
            campaign_id=row.campaign_id,
            user_id=user.id,
            metadata={"analysis_id": str(row.id)},
        )
        await self.session.commit()
        await self.session.refresh(row)
        return row
