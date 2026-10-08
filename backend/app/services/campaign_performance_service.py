"""Deterministic read-only evidence; no provider or queue dependencies."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign_kpi import CampaignKPI, KPIComparison, KPIMetric
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.services.campaign_kpi_service import CampaignKPIService

RAW_FIELDS = (
    "views",
    "impressions",
    "reactions",
    "likes",
    "comments",
    "shares",
    "clicks",
    "subscribers",
)


def required_fields(metric: KPIMetric) -> tuple[str, ...]:
    if metric == KPIMetric.CTR:
        return ("clicks", "impressions")
    if metric == KPIMetric.ENGAGEMENT_RATE:
        return ("reactions", "comments", "shares", "impressions")
    return (metric.value.lower(),)


def enough(values: dict[str, Any], metric: KPIMetric) -> bool:
    return all(values[field] is not None for field in required_fields(metric)) and (
        metric not in (KPIMetric.CTR, KPIMetric.ENGAGEMENT_RATE) or values["impressions"] > 0
    )


def sum_present(rows: list[dict[str, Any]], field: str) -> int | None:
    values = [row[field] for row in rows if row[field] is not None]
    return sum(values) if values else None


def observed(rows: list[dict[str, Any]], metric: KPIMetric) -> Decimal | None:
    # Derived metrics use complete publication observations only. Mixing missing
    # numerators and denominators across publications would fabricate a ratio.
    complete = [row for row in rows if enough(row, metric)]
    if not complete:
        return None
    if metric in (KPIMetric.CTR, KPIMetric.ENGAGEMENT_RATE):
        fields = required_fields(metric)
        denominator = sum(row["impressions"] for row in complete)
        numerator = sum(sum(row[field] for field in fields[:-1]) for row in complete)
        return Decimal(numerator) / Decimal(denominator)
    return Decimal(sum(row[metric.value.lower()] for row in complete))


class CampaignPerformanceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def rows(
        self,
        campaign_id: UUID,
        start: datetime,
        end: datetime,
        channel: ContentChannel | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            select(Publication)
            .where(
                Publication.campaign_id == campaign_id,
                Publication.status == PublicationStatus.PUBLISHED,
                Publication.published_at >= start.astimezone(UTC),
                Publication.published_at <= end.astimezone(UTC),
            )
            .order_by(Publication.published_at, Publication.id)
        )
        if channel is not None:
            query = query.where(Publication.channel == channel)
        pubs = list(await self.session.scalars(query))
        # Latest applicable snapshot is bounded by the requested observation end.
        snapshots = (
            list(
                await self.session.scalars(
                    select(PublicationMetricsSnapshot)
                    .where(
                        PublicationMetricsSnapshot.publication_id.in_([p.id for p in pubs]),
                        PublicationMetricsSnapshot.observed_at <= end.astimezone(UTC),
                    )
                    .order_by(
                        PublicationMetricsSnapshot.observed_at.desc(),
                        PublicationMetricsSnapshot.created_at.desc(),
                        PublicationMetricsSnapshot.id.desc(),
                    )
                )
            )
            if pubs
            else []
        )
        latest: dict[UUID, PublicationMetricsSnapshot] = {}
        for candidate in snapshots:
            latest.setdefault(candidate.publication_id, candidate)
        rows = []
        for pub in pubs:
            snapshot = latest.get(pub.id)
            rows.append(
                {
                    "publication_id": pub.id,
                    "content_item_id": pub.content_item_id,
                    "content_version_id": pub.content_version_id,
                    "channel": pub.channel,
                    "published_at": pub.published_at,
                    "metrics": snapshot,
                    "latest_metrics_snapshot_id": snapshot.id if snapshot else None,
                    "source": snapshot.source if snapshot else None,
                    "observed_at": snapshot.observed_at if snapshot else None,
                    **{
                        field: getattr(snapshot, field) if snapshot else None
                        for field in RAW_FIELDS
                    },
                }
            )
        return rows

    async def read(
        self,
        campaign_id: UUID,
        start: datetime,
        end: datetime,
        channel: ContentChannel | None = None,
    ) -> dict[str, Any]:
        await CampaignKPIService(self.session).campaign(campaign_id)
        rows = await self.rows(campaign_id, start, end, channel)
        count = len(rows)
        with_metrics = sum(any(row[field] is not None for field in RAW_FIELDS) for row in rows)
        kpis = list(
            await self.session.scalars(
                select(CampaignKPI)
                .where(CampaignKPI.campaign_id == campaign_id, CampaignKPI.is_active.is_(True))
                .order_by(CampaignKPI.created_at, CampaignKPI.id)
            )
        )
        evaluations = []
        for kpi in kpis:
            evidence = await self.rows(campaign_id, kpi.period_start, kpi.period_end, kpi.channel)
            value = observed(evidence, kpi.metric)
            eligible = len(evidence)
            sample = sum(enough(row, kpi.metric) for row in evidence)
            evaluations.append(
                {
                    "kpi_id": kpi.id,
                    "metric": kpi.metric,
                    "channel": kpi.channel,
                    "target_value": kpi.target_value,
                    "comparison": kpi.comparison,
                    "period_start": kpi.period_start,
                    "period_end": kpi.period_end,
                    "observed_value": value,
                    "target_met": None
                    if value is None
                    else (
                        value >= kpi.target_value
                        if kpi.comparison == KPIComparison.GTE
                        else value <= kpi.target_value
                    ),
                    "observed_publication_count": sample,
                    "eligible_publication_count": eligible,
                    "coverage_ratio": sample / eligible if eligible else 0,
                    "limitation": "Нет подходящих опубликованных публикаций."
                    if not eligible
                    else "Недостаточно исходных данных для метрики."
                    if not sample
                    else "Частичное покрытие: результат учитывает только полные наблюдения."
                    if sample < eligible
                    else None,
                }
            )
        return {
            "campaign_id": campaign_id,
            "period": {"from": start, "to": end},
            "data_quality": {
                "publication_count": count,
                "publications_with_any_metrics": with_metrics,
                "coverage_ratio": with_metrics / count if count else 0,
            },
            "total_published": count,
            "with_metrics": sum(row["metrics"] is not None for row in rows),
            "metric_coverage": {
                field: sum(row[field] is not None for row in rows) / count if count else 0
                for field in RAW_FIELDS
            },
            "totals": {field: sum_present(rows, field) for field in RAW_FIELDS},
            "derived": {
                "ctr": observed(rows, KPIMetric.CTR),
                "engagement_rate": observed(rows, KPIMetric.ENGAGEMENT_RATE),
            },
            "kpis": evaluations,
            "publications": rows,
        }
