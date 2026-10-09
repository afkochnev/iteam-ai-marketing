from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.integrations.metrics import (
    MetricsProvider,
    MetricsProviderError,
    TelegramMetricsProvider,
    VKMetricsProvider,
)
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.models.user import User
from app.services.activity_log_service import ActivityLogService

METRIC_FIELDS = (
    "views",
    "impressions",
    "reactions",
    "likes",
    "comments",
    "shares",
    "clicks",
    "subscribers",
)


class MetricsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _publication(self, publication_id: UUID) -> Publication:
        publication = await self.session.get(Publication, publication_id)
        if publication is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        if publication.status is not PublicationStatus.PUBLISHED:
            raise AppError(
                "METRICS_PUBLICATION_NOT_PUBLISHED",
                "Метрики доступны только для опубликованного контента.",
                409,
            )
        if not publication.external_id:
            raise AppError(
                "METRICS_EXTERNAL_ID_REQUIRED",
                "У публикации отсутствует внешний идентификатор.",
                409,
            )
        return publication

    @staticmethod
    def _validate_values(values: dict[str, int | None]) -> None:
        if any(
            value is not None and (not isinstance(value, int) or value < 0)
            for value in values.values()
        ):
            raise AppError(
                "METRICS_INVALID_VALUE", "Метрики должны быть целыми неотрицательными числами.", 422
            )

    async def _latest(
        self, publication_id: UUID, source: MetricsSource, provider: str | None
    ) -> PublicationMetricsSnapshot | None:
        return cast(
            PublicationMetricsSnapshot | None,
            await self.session.scalar(
                select(PublicationMetricsSnapshot)
                .where(
                    PublicationMetricsSnapshot.publication_id == publication_id,
                    PublicationMetricsSnapshot.source == source,
                    PublicationMetricsSnapshot.provider == provider,
                )
                .order_by(
                    PublicationMetricsSnapshot.observed_at.desc(),
                    PublicationMetricsSnapshot.created_at.desc(),
                    PublicationMetricsSnapshot.id.desc(),
                )
                .limit(1)
            ),
        )

    async def _store(
        self,
        publication: Publication,
        *,
        source: MetricsSource,
        observed_at: datetime,
        provider: str | None,
        values: dict[str, int | None],
        user: User | None = None,
        note: str | None = None,
    ) -> PublicationMetricsSnapshot:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise AppError(
                "METRICS_TIMEZONE_REQUIRED", "Время наблюдения должно содержать timezone.", 422
            )
        self._validate_values(values)
        observed_at = observed_at.astimezone(UTC)
        latest = await self._latest(publication.id, source, provider)
        if (
            latest is not None
            and all(getattr(latest, field) == values.get(field) for field in METRIC_FIELDS)
            and latest.source is source
            and latest.provider == provider
        ):
            return latest
        snapshot = PublicationMetricsSnapshot(
            publication_id=publication.id,
            channel=publication.channel,
            observed_at=observed_at,
            source=source,
            provider=provider,
            metadata_={"note": note} if note else {},
            **values,
        )
        self.session.add(snapshot)
        await self.session.flush()
        event = (
            "PUBLICATION_METRICS_RECORDED_MANUALLY"
            if source is MetricsSource.MANUAL
            else "PUBLICATION_METRICS_SYNCED"
        )
        await ActivityLogService(self.session).record(
            event,
            operation_key=f"publication-metrics:{snapshot.id}",
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            metadata={
                "publication_id": str(publication.id),
                "snapshot_id": str(snapshot.id),
                "source": source.value,
                "observed_at": observed_at.isoformat(),
                "fields": [field for field in METRIC_FIELDS if values.get(field) is not None],
            },
        )
        await self.session.commit()
        await self.session.refresh(snapshot)
        return snapshot

    async def record_manual(
        self,
        publication_id: UUID,
        user: User,
        *,
        observed_at: datetime,
        values: dict[str, int | None],
        note: str | None = None,
    ) -> PublicationMetricsSnapshot:
        publication = await self._publication(publication_id)
        return await self._store(
            publication,
            source=MetricsSource.MANUAL,
            observed_at=observed_at,
            provider=None,
            values=values,
            user=user,
            note=note,
        )

    async def sync(self, publication_id: UUID) -> PublicationMetricsSnapshot:
        publication = await self._publication(publication_id)
        if publication.channel is ContentChannel.TELEGRAM:
            provider: MetricsProvider = TelegramMetricsProvider()
        elif publication.channel is ContentChannel.VK:
            provider = VKMetricsProvider()
        else:
            raise AppError("METRICS_CHANNEL_UNSUPPORTED", "Канал не поддерживает метрики.", 409)
        try:
            result = await provider.get_metrics(external_id=publication.external_id or "")
        except MetricsProviderError as error:
            await ActivityLogService(self.session).record(
                "PUBLICATION_METRICS_SYNC_FAILED",
                operation_key=f"publication-metrics-failed:{publication.id}:{datetime.now(UTC).isoformat()}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={"publication_id": str(publication.id), "code": error.code},
            )
            await self.session.commit()
            raise AppError(error.code, error.safe_message, 502) from None
        return await self._store(
            publication,
            source=MetricsSource.PROVIDER,
            observed_at=result.observed_at,
            provider=result.provider,
            values={field: getattr(result, field) for field in METRIC_FIELDS},
        )

    async def publication_metrics(self, publication_id: UUID) -> dict[str, Any]:
        publication = await self.session.get(Publication, publication_id)
        if publication is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        snapshots = list(
            (
                await self.session.scalars(
                    select(PublicationMetricsSnapshot)
                    .where(PublicationMetricsSnapshot.publication_id == publication_id)
                    .order_by(PublicationMetricsSnapshot.observed_at.desc())
                )
            ).all()
        )
        return {
            "publication_id": publication_id,
            "sync_capable": False,
            "latest": snapshots[0] if snapshots else None,
            "history": snapshots,
        }

    async def campaign_performance(
        self,
        campaign_id: UUID,
        start: datetime,
        end: datetime,
        channel: ContentChannel | None = None,
    ) -> dict[str, Any]:
        if start.tzinfo is None or end.tzinfo is None:
            raise AppError("METRICS_TIMEZONE_REQUIRED", "Диапазон должен содержать timezone.", 422)
        if end <= start:
            raise AppError(
                "METRICS_INVALID_RANGE", "Конец диапазона должен быть позже начала.", 422
            )
        if end - start > timedelta(days=180):
            raise AppError("METRICS_RANGE_TOO_LARGE", "Диапазон не может превышать 180 дней.", 422)
        from app.services.campaign_performance_service import CampaignPerformanceService

        return await CampaignPerformanceService(self.session).read(campaign_id, start, end, channel)
