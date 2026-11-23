from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.metrics import (
    MetricsProvider,
    MetricsProviderError,
    TelegramMetricsProvider,
    VKMetricsProvider,
)
from app.models.content import ContentChannel
from app.models.publication import (
    LegacyExternalPlacement,
    Publication,
    PublicationClaimEvent,
    PublicationStatus,
)
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
        publication = await self.session.get(Publication, publication_id, populate_existing=True)
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

    async def _placement_id(
        self, publication: Publication, *, reject_conflict: bool = False
    ) -> UUID | None:
        if (
            publication.status is not PublicationStatus.PUBLISHED
            or publication.provider_target_id is None
            or publication.external_id is None
        ):
            return None
        # Legacy decimal IDs can contain leading zeroes. The adapters look them up as integers.
        object_id = publication.external_id.lstrip("0")
        match = (
            await self.session.execute(
                select(
                    LegacyExternalPlacement.id,
                    PublicationClaimEvent.publication_id,
                    PublicationClaimEvent.channel,
                    PublicationClaimEvent.content_version_id,
                )
                .join(
                    PublicationClaimEvent,
                    PublicationClaimEvent.id == LegacyExternalPlacement.claim_event_id,
                )
                .where(
                    LegacyExternalPlacement.channel == publication.channel,
                    LegacyExternalPlacement.provider_target_id == publication.provider_target_id,
                    LegacyExternalPlacement.external_id == object_id,
                )
            )
        ).one_or_none()
        if match is None:
            return None
        placement_id, owner_id, channel, version_id = match._tuple()
        if (owner_id, channel, version_id) == (
            publication.id,
            publication.channel,
            publication.content_version_id,
        ):
            return placement_id if object_id == publication.external_id else None
        if reject_conflict:
            raise AppError(
                "METRICS_PLACEMENT_CONFLICT",
                "Внешнее размещение закреплено за другой публикацией или версией.",
                409,
            )
        return None

    async def _latest(
        self,
        publication_id: UUID,
        source: MetricsSource,
        provider: str | None,
        placement_id: UUID | None,
    ) -> PublicationMetricsSnapshot | None:
        return cast(
            PublicationMetricsSnapshot | None,
            await self.session.scalar(
                select(PublicationMetricsSnapshot)
                .where(
                    PublicationMetricsSnapshot.publication_id == publication_id,
                    PublicationMetricsSnapshot.source == source,
                    PublicationMetricsSnapshot.provider == provider,
                    PublicationMetricsSnapshot.external_placement_id == placement_id,
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
        placement_id: UUID | None = None,
        user: User | None = None,
        note: str | None = None,
    ) -> PublicationMetricsSnapshot:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise AppError(
                "METRICS_TIMEZONE_REQUIRED", "Время наблюдения должно содержать timezone.", 422
            )
        self._validate_values(values)
        observed_at = observed_at.astimezone(UTC)
        latest = await self._latest(publication.id, source, provider, placement_id)
        if (
            latest is not None
            and all(getattr(latest, field) == values.get(field) for field in METRIC_FIELDS)
            and latest.source is source
            and latest.provider == provider
        ):
            if source is MetricsSource.PROVIDER:
                await self.session.commit()  # Release the publication lock on a duplicate read.
            return latest
        snapshot = PublicationMetricsSnapshot(
            publication_id=publication.id,
            external_placement_id=placement_id,
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
        # An unchanged read reuses the previous snapshot. Serialize sampling as well as
        # storage so a delayed older observation cannot become the latest visible value.
        try:
            await self.session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"iteam:publication-metrics:{publication_id}", 0)
                    )
                )
            )
            return await self._sync_provider(publication_id)
        except BaseException:
            # Cancellation and validation failures must release the transaction-scoped lock.
            await self.session.rollback()
            raise

    async def _sync_provider(self, publication_id: UUID) -> PublicationMetricsSnapshot:
        publication = await self._publication(publication_id)
        expected_target: str | None = None
        if publication.channel is ContentChannel.TELEGRAM:
            provider: MetricsProvider = TelegramMetricsProvider()
            provider_name = "telegram_mtproto"
            if settings.telegram_metrics_enabled and settings.telegram_metrics_chat_id is not None:
                expected_target = str(settings.telegram_metrics_chat_id)
        elif publication.channel is ContentChannel.VK:
            provider = VKMetricsProvider()
            provider_name = "vk_wall"
            if settings.vk_metrics_enabled and settings.vk_metrics_owner_id is not None:
                expected_target = str(settings.vk_metrics_owner_id)
        else:
            raise AppError("METRICS_CHANNEL_UNSUPPORTED", "Канал не поддерживает метрики.", 409)
        if expected_target is not None:
            if publication.provider_target_id is None:
                raise AppError(
                    "METRICS_PROVIDER_TARGET_UNBOUND",
                    "Публикация не привязана к точной внешней площадке.",
                    409,
                )
            if publication.provider_target_id != expected_target:
                raise AppError(
                    "METRICS_PROVIDER_TARGET_MISMATCH",
                    "Публикация относится к другой внешней площадке.",
                    409,
                )
        channel = publication.channel
        version_id = publication.content_version_id
        target_id = publication.provider_target_id
        external_id = publication.external_id or ""
        try:
            result = await provider.get_metrics(external_id=external_id)
            verified = result.provider_target_id is not None or result.external_id is not None
            if verified and (
                result.provider != provider_name
                or result.provider_target_id is None
                or result.provider_target_id != target_id
                or result.external_id != external_id
            ):
                raise MetricsProviderError(
                    "METRICS_PROVIDER_TARGET_MISMATCH",
                    "Ответ провайдера относится к другой внешней публикации.",
                )
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
        values = {field: getattr(result, field) for field in METRIC_FIELDS}
        if all(value is None for value in values.values()):
            await ActivityLogService(self.session).record(
                "PUBLICATION_METRICS_SYNC_FAILED",
                operation_key=f"publication-metrics-no-data:{publication.id}:{datetime.now(UTC).isoformat()}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={"publication_id": str(publication.id), "code": "METRICS_NO_DATA"},
            )
            await self.session.commit()
            raise AppError(
                "METRICS_NO_DATA",
                "Провайдер не вернул ни одной наблюдаемой метрики.",
                502,
            )
        # The advisory lock only covers metrics readers. A delivery or correction may
        # have changed the publication while the provider was responding.
        locked = await self.session.scalar(
            select(Publication)
            .where(
                Publication.id == publication_id,
                Publication.status == PublicationStatus.PUBLISHED,
                Publication.channel == channel,
                Publication.content_version_id == version_id,
                Publication.provider_target_id == target_id,
                Publication.external_id == external_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise AppError(
                "METRICS_PUBLICATION_CHANGED", "Публикация изменилась во время чтения метрик.", 409
            )
        placement_id = await self._placement_id(locked, reject_conflict=True)
        return await self._store(
            locked,
            placement_id=placement_id if verified else None,
            source=MetricsSource.PROVIDER,
            observed_at=result.observed_at,
            provider=result.provider,
            values=values,
        )

    async def publication_metrics(self, publication_id: UUID) -> dict[str, Any]:
        publication = await self.session.get(Publication, publication_id, populate_existing=True)
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
            "external_placement_id": await self._placement_id(publication),
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
