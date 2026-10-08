from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.marketing_feedback import FeedbackAnalysisStatus, MarketingFeedbackAnalysis


async def fail_linked_analysis(session: AsyncSession, run: AgentRun) -> None:
    row = await session.scalar(
        select(MarketingFeedbackAnalysis)
        .where(
            MarketingFeedbackAnalysis.agent_run_id == run.id,
            MarketingFeedbackAnalysis.task_id == run.task_id,
        )
        .with_for_update()
    )
    if row is not None and row.status is FeedbackAnalysisStatus.DRAFT:
        row.status = FeedbackAnalysisStatus.FAILED
        from app.services.feedback_service import FeedbackService

        await FeedbackService(session).analysis_event(row, "FEEDBACK_ANALYSIS_FAILED")
