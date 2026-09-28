from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.campaign import Campaign
from app.models.content import ContentItem, ContentVersion
from app.models.marketing_feedback import (
    FeedbackAnalysisStatus,
    FeedbackSource,
    MarketingFeedback,
    MarketingFeedbackAnalysis,
)
from app.models.publication import Publication
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.repositories.agents import AgentRepository
from app.schemas.feedback import FeedbackAnalystResult
from app.services.activity_log_service import ActivityLogService


class FeedbackService:
    def __init__(self, session: AsyncSession):
        self.session = session

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
                    .order_by(MarketingFeedback.created_at.desc())
                )
            ).all()
        )

    async def _input_snapshot(self, campaign_id: UUID) -> dict[str, Any]:
        feedback = await self.list_feedback(campaign_id)
        publications = list(
            (
                await self.session.scalars(
                    select(Publication).where(Publication.campaign_id == campaign_id)
                )
            ).all()
        )
        publication_ids = [row.id for row in publications]
        snapshots: list[PublicationMetricsSnapshot] = []
        for publication_id in publication_ids:
            latest = await self.session.scalar(
                select(PublicationMetricsSnapshot)
                .where(PublicationMetricsSnapshot.publication_id == publication_id)
                .order_by(PublicationMetricsSnapshot.observed_at.desc())
                .limit(1)
            )
            if latest:
                snapshots.append(latest)
        campaign = await self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        return {
            "campaign_id": str(campaign_id),
            "strategy_version": campaign.strategy_version,
            "strategy_reference": {
                "campaign_id": str(campaign_id),
                "strategy_version": campaign.strategy_version,
            },
            "publication_ids": [str(row.id) for row in publications],
            "content_version_ids": [str(row.content_version_id) for row in publications],
            "metrics_snapshot_ids": [str(row.id) for row in snapshots],
            "metric_sources": sorted({str(row.source) for row in snapshots}),
            "feedback_ids": [str(row.id) for row in feedback],
            "publications_analyzed": len(publications),
            "feedback_count": len(feedback),
            "metric_coverage_ratio": (len(snapshots) / len(publications) if publications else 0.0),
        }

    async def _build_analysis(
        self,
        campaign_id: UUID,
        snapshot: dict[str, Any],
        analysis: MarketingFeedbackAnalysis,
    ) -> MarketingFeedbackAnalysis:
        analysis_key = f"feedback-analysis:{campaign_id}:{datetime.now(UTC).isoformat()}"
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_QUEUED",
            operation_key=f"{analysis_key}:queued",
            campaign_id=campaign_id,
            metadata={"evidence_count": len(snapshot["feedback_ids"])},
        )
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_STARTED",
            operation_key=f"{analysis_key}:started",
            campaign_id=campaign_id,
            metadata={"evidence_count": len(snapshot["feedback_ids"])},
        )
        findings: list[dict[str, Any]] = []
        recommendations: list[dict[str, Any]] = []
        limitations: list[str] = []
        if not snapshot["publications_analyzed"] and not snapshot["feedback_count"]:
            limitations.append("Недостаточно данных для уверенного вывода.")
        if snapshot["metric_coverage_ratio"] < 1:
            limitations.append("Метрики доступны только для части публикаций.")
        for feedback_id in snapshot["feedback_ids"]:
            findings.append(
                {
                    "type": "HUMAN_FEEDBACK",
                    "evidence_refs": [feedback_id],
                    "observation": "Учтена ручная обратная связь.",
                    "confidence": "provided",
                }
            )
        if not findings:
            limitations.append("Вывод основан только на доступных количественных наблюдениях.")
        analysis.status = FeedbackAnalysisStatus.DRAFT
        analysis.summary = analysis.summary or (
            "Анализ основан на зафиксированных наблюдениях и не заменяет экспертное решение."
        )
        analysis.findings = findings
        analysis.recommendations = recommendations
        analysis.experiment_ideas = []
        analysis.limitations = limitations
        analysis.generated_at = datetime.now(UTC)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_COMPLETED",
            operation_key=f"feedback-analysis-completed:{analysis.id}",
            campaign_id=campaign_id,
            metadata={"analysis_id": str(analysis.id), "evidence_count": len(findings)},
        )
        return analysis

    async def generate_analysis(self, campaign_id: UUID) -> MarketingFeedbackAnalysis:
        """Synchronous helper retained for deterministic unit tests only."""
        snapshot = await self._input_snapshot(campaign_id)
        row = MarketingFeedbackAnalysis(
            campaign_id=campaign_id,
            status=FeedbackAnalysisStatus.DRAFT,
            strategy_version=int(snapshot["strategy_version"]),
            summary="",
            input_snapshot=snapshot,
            findings=[],
            recommendations=[],
            experiment_ideas=[],
            limitations=[],
        )
        self.session.add(row)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_QUEUED",
            operation_key=f"feedback-analysis-queued:{row.id}",
            campaign_id=campaign_id,
            metadata={
                "analysis_id": str(row.id),
                "evidence_count": len(snapshot["feedback_ids"]),
            },
        )
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_STARTED",
            operation_key=f"feedback-analysis-started:{row.id}",
            campaign_id=campaign_id,
            metadata={"analysis_id": str(row.id)},
        )
        row = await self._build_analysis(campaign_id, snapshot, row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def queue_analysis(self, campaign_id: UUID) -> MarketingFeedbackAnalysis:
        snapshot = await self._input_snapshot(campaign_id)
        agent = await AgentRepository(self.session).get_by_slug("marketing_director")
        if agent is None:
            agent = await self.session.scalar(
                select(Agent).where(Agent.status == AgentStatus.ACTIVE).order_by(Agent.created_at)
            )
        if agent is None:
            raise AppError(
                "FEEDBACK_ANALYST_UNAVAILABLE",
                "Аналитик обратной связи недоступен.",
                409,
            )
        task = Task(
            campaign_id=campaign_id,
            task_type=TaskType.MANUAL,
            title="Проанализировать обратную связь кампании",
            description="Сформировать структурированные консультативные выводы.",
            assigned_agent_id=agent.id,
            status=TaskStatus.READY,
            input_data={"feedback_analysis": True},
            output_data={},
        )
        self.session.add(task)
        await self.session.flush()
        run = AgentRun(
            agent_id=agent.id,
            task_id=task.id,
            campaign_id=campaign_id,
            status=AgentRunStatus.QUEUED,
            input_data={"feedback_snapshot": snapshot},
            model=agent.model or "feedback-analyst",
            prompt_snapshot="Feedback Analyst: advisory analysis only.",
            prompt_hash="feedback-analyst",
        )
        self.session.add(run)
        await self.session.flush()
        row = MarketingFeedbackAnalysis(
            campaign_id=campaign_id,
            status=FeedbackAnalysisStatus.DRAFT,
            strategy_version=int(snapshot["strategy_version"]),
            summary="",
            input_snapshot=snapshot,
            findings=[],
            recommendations=[],
            experiment_ideas=[],
            limitations=[],
            agent_run_id=run.id,
        )
        self.session.add(row)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_QUEUED",
            operation_key=f"feedback-analysis-queued:{row.id}",
            campaign_id=campaign_id,
            metadata={"analysis_id": str(row.id), "agent_run_id": str(run.id)},
        )
        await self.session.commit()
        try:
            from app.workers.feedback_worker import generate_feedback_analysis

            result = generate_feedback_analysis.delay(str(run.id))
            run.queue_job_id = result.id
            await self.session.commit()
        except Exception as exc:
            run.status = AgentRunStatus.FAILED
            run.error_code = "QUEUE_ENQUEUE_FAILED"
            run.error_message = "Не удалось поставить анализ в очередь."
            row.status = FeedbackAnalysisStatus.FAILED
            await self.session.commit()
            raise AppError(
                "QUEUE_ENQUEUE_FAILED", "Не удалось поставить анализ в очередь.", 503
            ) from exc
        await self.session.refresh(row)
        return row

    async def complete_queued_analysis(
        self, analysis_id: UUID, run_id: UUID
    ) -> MarketingFeedbackAnalysis:
        row = await self.session.get(MarketingFeedbackAnalysis, analysis_id, with_for_update=True)
        if row is None or row.agent_run_id != run_id:
            raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
        snapshot = dict(row.input_snapshot)
        return await self._build_analysis(row.campaign_id, snapshot, row)

    @staticmethod
    def validate_evidence(snapshot: dict[str, Any], result: FeedbackAnalystResult) -> None:
        allowed = {
            "publication": set(snapshot["publication_ids"]),
            "content_version": set(snapshot["content_version_ids"]),
            "metrics_snapshot": set(snapshot["metrics_snapshot_ids"]),
            "marketing_feedback": set(snapshot["feedback_ids"]),
        }
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
        self.validate_evidence(row.input_snapshot, result)
        row.summary = result.summary
        row.findings = [item.model_dump(mode="json") for item in result.findings]
        row.recommendations = [item.model_dump(mode="json") for item in result.recommendations]
        row.experiment_ideas = [item.model_dump(mode="json") for item in result.experiment_ideas]
        row.limitations = result.limitations
        row.generated_at = datetime.now(UTC)
        row.status = FeedbackAnalysisStatus.DRAFT
        await ActivityLogService(self.session).record(
            "FEEDBACK_ANALYSIS_COMPLETED",
            operation_key=f"feedback-analysis-completed:{row.id}",
            campaign_id=row.campaign_id,
            metadata={"analysis_id": str(row.id), "evidence_count": len(row.findings)},
        )
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
        row = await self.session.get(MarketingFeedbackAnalysis, analysis_id, with_for_update=True)
        if row is None:
            raise AppError("FEEDBACK_ANALYSIS_NOT_FOUND", "Анализ не найден.", 404)
        if row.status is not FeedbackAnalysisStatus.DRAFT:
            if row.status is status:
                return row
            raise AppError("FEEDBACK_ANALYSIS_ALREADY_REVIEWED", "Анализ уже рассмотрен.", 409)
        row.status = status
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
