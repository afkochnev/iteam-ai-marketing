from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis
from app.services.activity_log_service import ActivityLogService
from app.services.feedback_service import FeedbackService


class LegacyFeedbackFixture(FeedbackService):
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
                "evidence_fingerprint": row.evidence_fingerprint,
                "interpretations": row.interpretations,
                "campaign_kpis": row.input_snapshot.get("campaign_kpis", []),
                "analysis_period_start": row.input_snapshot.get("analysis_period_start"),
                "analysis_period_end": row.input_snapshot.get("analysis_period_end"),
                "data_quality": row.input_snapshot.get("data_quality", {}),
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


async def prepared_analysis(session, campaign_id, *, allow_missing_model=False):
    from app.core.config import settings
    from app.models.agent import Agent, AgentStatus
    from app.repositories.agents import AgentRepository
    from app.services.agent_run_service import AgentRunService

    repository = AgentRepository(session)
    if await repository.get_by_slug("marketing_analyst") is None:
        session.add(
            Agent(
                name="Marketing Analyst",
                slug="marketing_analyst",
                role="marketing_analyst",
                system_prompt="Analyze frozen evidence only",
                model=None
                if settings.openai_default_model or allow_missing_model
                else "test-model",
                status=AgentStatus.ACTIVE,
                autonomy_level=1,
                settings={},
            )
        )
        await session.commit()
    row = await FeedbackService(session).queue_analysis(campaign_id)
    agent = await repository.get_by_slug("marketing_analyst")
    if agent.model or settings.openai_default_model:
        run = await AgentRunService(session).create_queued_run(row.task_id)
        await AgentRunService(session).enqueue(run)
    await session.refresh(row)
    return row
