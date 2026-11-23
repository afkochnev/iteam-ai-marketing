"""Read legacy publication details and calendar entries using the caller's session."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.models.content import ContentDerivation, ContentItem
from app.models.publication import Publication, PublicationReconciliation, PublicationStatus
from app.schemas.publication import (
    PublicationCalendarItem,
    PublicationProvenance,
    PublicationReconciliationResponse,
    PublicationResponse,
)
from app.services.publication_operations import operational_fields


class PublicationReadService:
    def __init__(self, session: AsyncSession, has_canary_retry: Callable[[UUID], Awaitable[bool]]):
        self.session = session
        self._has_canary_retry = has_canary_retry

    async def get(self, publication_id: UUID) -> PublicationResponse:
        publication = await self.session.get(Publication, publication_id)
        if publication is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        return await self.response(publication)

    async def list_campaign(self, campaign_id: UUID) -> list[PublicationResponse]:
        publications = list(
            (
                await self.session.scalars(
                    select(Publication)
                    .where(Publication.campaign_id == campaign_id)
                    .order_by(Publication.created_at.desc())
                )
            ).all()
        )
        return [await self.response(publication) for publication in publications]

    async def calendar(
        self, campaign_id: UUID, from_at: datetime, to_at: datetime
    ) -> list[PublicationCalendarItem]:
        """Return operational publication rows intersecting a bounded date range."""
        self._validate_calendar_range(from_at, to_at)
        start = from_at.astimezone(UTC)
        end = to_at.astimezone(UTC)
        scheduled_in_range = and_(
            Publication.scheduled_at.is_not(None),
            Publication.scheduled_at >= start,
            Publication.scheduled_at <= end,
        )
        published_in_range = and_(
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.published_at.is_not(None),
            Publication.published_at >= start,
            Publication.published_at <= end,
        )
        rows = list(
            (
                await self.session.execute(
                    select(Publication, ContentItem.title)
                    .join(ContentItem, ContentItem.id == Publication.content_item_id)
                    .where(
                        Publication.campaign_id == campaign_id,
                        or_(scheduled_in_range, published_in_range),
                    )
                    .order_by(
                        Publication.scheduled_at.asc().nulls_last(),
                        Publication.published_at.asc().nulls_last(),
                    )
                )
            ).all()
        )
        return [
            PublicationCalendarItem(
                **operational_fields(publication),
                publication_id=publication.id,
                content_item_id=publication.content_item_id,
                content_version_id=publication.content_version_id,
                title=title,
                channel=publication.channel,
                status=publication.status,
                scheduled_at=publication.scheduled_at,
                published_at=publication.published_at,
                external_url=publication.external_url,
                provider_enabled=(
                    settings.telegram_publishing_enabled
                    if publication.channel.value == "TELEGRAM"
                    else settings.vk_publishing_enabled
                ),
                failure_code=publication.failure_code,
            )
            for publication, title in rows
        ]

    @staticmethod
    def _validate_calendar_range(from_at: datetime, to_at: datetime) -> None:
        if from_at.tzinfo is None or from_at.utcoffset() is None:
            raise AppError(
                "PUBLICATION_TIMEZONE_REQUIRED", "Диапазон должен содержать timezone.", 422
            )
        if to_at.tzinfo is None or to_at.utcoffset() is None:
            raise AppError(
                "PUBLICATION_TIMEZONE_REQUIRED", "Диапазон должен содержать timezone.", 422
            )
        if to_at <= from_at:
            raise AppError(
                "PUBLICATION_INVALID_RANGE", "Конец диапазона должен быть позже начала.", 422
            )
        if to_at - from_at > timedelta(days=90):
            raise AppError(
                "PUBLICATION_RANGE_TOO_LARGE",
                "Диапазон календаря не может превышать 90 дней.",
                422,
            )

    async def response(self, publication: Publication) -> PublicationResponse:
        history = list(
            (
                await self.session.scalars(
                    select(PublicationReconciliation)
                    .where(PublicationReconciliation.publication_id == publication.id)
                    .order_by(PublicationReconciliation.created_at.asc())
                )
            ).all()
        )
        provider_enabled = (
            settings.telegram_publishing_enabled
            if publication.channel.value == "TELEGRAM"
            else settings.vk_publishing_enabled
        )
        reconciliation_required = publication.failure_code in {
            "TELEGRAM_RECONCILIATION_REQUIRED",
            "VK_RECONCILIATION_REQUIRED",
        }
        retry_allowed = (
            publication.status is PublicationStatus.FAILED
            and publication.failure_code
            in {
                "TELEGRAM_RATE_LIMIT",
                "TELEGRAM_PROVIDER_TIMEOUT",
                "TELEGRAM_PROVIDER_ERROR",
                "VK_RATE_LIMIT",
                "VK_PROVIDER_ERROR",
                "PUBLICATION_RECONCILED_NOT_PUBLISHED",
            }
            and provider_enabled
            and publication.retry_count < settings.publication_max_retries
        )
        retry_allowed = retry_allowed and not await self._has_canary_retry(publication.id)
        content_item = await self.session.get(ContentItem, publication.content_item_id)
        raw_plan_item = (
            (
                (content_item.metadata_ or {}).get("publication_plan_item_id")
                or (content_item.metadata_ or {}).get("channel_change_origin_plan_item_id")
            )
            if content_item
            else None
        )
        try:
            plan_item_id = UUID(str(raw_plan_item)) if raw_plan_item else None
        except ValueError:
            plan_item_id = None
        return PublicationResponse(
            **operational_fields(publication),
            id=publication.id,
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            content_version_id=publication.content_version_id,
            channel=publication.channel,
            provider_enabled=provider_enabled,
            publication_plan_item_id=plan_item_id,
            status=publication.status,
            scheduled_at=publication.scheduled_at,
            approved_for_publish_at=publication.approved_for_publish_at,
            approved_for_publish_by=publication.approved_for_publish_by,
            external_id=publication.external_id,
            provider_target_id=publication.provider_target_id,
            external_url=publication.external_url,
            published_at=publication.published_at,
            failure_code=publication.failure_code,
            failure_message=publication.failure_message,
            retry_count=publication.retry_count,
            created_at=publication.created_at,
            updated_at=publication.updated_at,
            provenance=await self._provenance(publication.content_version_id),
            retry_allowed=retry_allowed,
            reconciliation_required=reconciliation_required,
            reconciliation_history=[
                PublicationReconciliationResponse.model_validate(item) for item in history
            ],
        )

    async def _provenance(self, version_id: UUID) -> list[PublicationProvenance]:
        rows = (
            (
                await self.session.execute(
                    select(ContentDerivation).where(
                        ContentDerivation.derived_content_version_id == version_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            PublicationProvenance(
                content_version_id=version_id,
                source_content_version_id=row.source_content_version_id,
                section_key=row.source_section_key,
            )
            for row in rows
        ]
