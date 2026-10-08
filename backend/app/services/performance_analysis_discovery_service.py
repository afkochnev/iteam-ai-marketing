from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import String, cast, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.errors import AppError
from app.models.campaign import Campaign, CampaignStatus
from app.models.marketing_feedback import (
    AnalysisTriggerSource,
    FeedbackAnalysisStatus,
    MarketingFeedback,
    MarketingFeedbackAnalysis,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.services.feedback_service import FeedbackService


class PerformanceAnalysisDiscoveryService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def discover(self, *, limit: int = 20) -> list[UUID]:
        if limit <= 0:
            return []
        limit = min(limit, 100)
        latest = (
            select(func.max(MarketingFeedbackAnalysis.created_at))
            .where(MarketingFeedbackAnalysis.campaign_id == Campaign.id)
            .correlate(Campaign)
            .scalar_subquery()
        )
        active = (
            select(MarketingFeedbackAnalysis.id)
            .where(
                MarketingFeedbackAnalysis.campaign_id == Campaign.id,
                MarketingFeedbackAnalysis.status == FeedbackAnalysisStatus.DRAFT,
            )
            .exists()
        )
        previous_snapshot = (
            select(MarketingFeedbackAnalysis.input_snapshot)
            .where(MarketingFeedbackAnalysis.campaign_id == Campaign.id)
            .order_by(
                MarketingFeedbackAnalysis.created_at.desc(), MarketingFeedbackAnalysis.id.desc()
            )
            .correlate(Campaign)
            .limit(1)
            .scalar_subquery()
        )
        newer = aliased(PublicationMetricsSnapshot)
        has_newer = (
            select(newer.id)
            .where(
                newer.publication_id == PublicationMetricsSnapshot.publication_id,
                tuple_(newer.observed_at, newer.created_at, newer.id)
                > tuple_(
                    PublicationMetricsSnapshot.observed_at,
                    PublicationMetricsSnapshot.created_at,
                    PublicationMetricsSnapshot.id,
                ),
            )
            .correlate(PublicationMetricsSnapshot)
            .exists()
        )
        metrics = (
            select(PublicationMetricsSnapshot.id)
            .join(Publication, Publication.id == PublicationMetricsSnapshot.publication_id)
            .where(
                Publication.campaign_id == Campaign.id,
                Publication.status == PublicationStatus.PUBLISHED,
                ~has_newer,
                or_(
                    previous_snapshot.is_(None),
                    ~previous_snapshot["metrics_snapshot_ids"].contains(
                        func.jsonb_build_array(cast(PublicationMetricsSnapshot.id, String))
                    ),
                ),
            )
            .correlate(Campaign)
            .exists()
        )
        feedback = (
            select(MarketingFeedback.id)
            .where(
                MarketingFeedback.campaign_id == Campaign.id,
                or_(
                    previous_snapshot.is_(None),
                    ~previous_snapshot["feedback_ids"].contains(
                        func.jsonb_build_array(cast(MarketingFeedback.id, String))
                    ),
                ),
            )
            .correlate(Campaign)
            .exists()
        )
        cutoff = datetime.now(UTC) - timedelta(hours=settings.optimization_analysis_cooldown_hours)
        # Compare evidence identity, not created_at: a transaction may commit late.
        # Unchanged campaigns do not occupy the bounded discovery batch.
        ids = list(
            await self.session.scalars(
                select(Campaign.id)
                .where(
                    Campaign.status != CampaignStatus.ARCHIVED,
                    ~active,
                    or_(latest.is_(None), latest <= cutoff),
                    or_(metrics, feedback),
                )
                .order_by(latest.asc().nullsfirst(), Campaign.id)
                .limit(limit)
            )
        )
        await self.session.commit()
        created = []
        for campaign_id in ids:
            try:
                service = FeedbackService(self.session)
                row = await service.prepare_analysis(
                    campaign_id, AnalysisTriggerSource.AUTOMATIC, automatic=True
                )
                if row is not None and service.prepared_new:
                    created.append(row.id)
            except AppError:
                await self.session.rollback()
        return created
