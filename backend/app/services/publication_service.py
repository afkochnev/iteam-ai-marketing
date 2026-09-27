from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.telegram import (
    PublicationProvider,
    TelegramProvider,
    TelegramProviderError,
    VKProvider,
    VKProviderError,
)
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication import (
    ACTIVE_PUBLICATION_STATUSES,
    PUBLICATION_ALLOWED_TRANSITIONS,
    Publication,
    PublicationStatus,
)
from app.models.user import User
from app.schemas.publication import (
    PublicationCalendarItem,
    PublicationCreate,
    PublicationProvenance,
    PublicationResponse,
)
from app.services.activity_log_service import ActivityLogService


def utc_now() -> datetime:
    """Authoritative application clock; patched by deterministic tests."""
    return datetime.now(UTC)


class PublicationService:
    def __init__(self, session: AsyncSession):
        self.session = session

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

    async def _ensure_content_version_approved(
        self, content_item_id: UUID, content_version_id: UUID, channel: object
    ) -> ContentItem:
        item = (
            await self.session.execute(
                select(ContentItem).where(ContentItem.id == content_item_id).with_for_update()
            )
        ).scalar_one_or_none()
        if item is None:
            raise AppError("CONTENT_NOT_FOUND", "Материал не найден.", 404)
        if item.content_type is not ContentType.SOCIAL_POST:
            raise AppError(
                "PUBLICATION_CONTENT_TYPE_INVALID",
                "Публикация доступна только для Social Post.",
                422,
            )
        if item.status is not ContentStatus.APPROVED:
            raise AppError(
                "PUBLICATION_CONTENT_NOT_APPROVED",
                "Social Post должен быть согласован до подготовки публикации.",
                409,
            )
        if item.channel is not channel:
            raise AppError(
                "PUBLICATION_CHANNEL_MISMATCH", "Канал публикации не совпадает с постом.", 409
            )
        version = await self.session.scalar(
            select(ContentVersion).where(
                ContentVersion.id == content_version_id,
                ContentVersion.content_item_id == content_item_id,
            )
        )
        if version is None:
            raise AppError(
                "PUBLICATION_VERSION_MISMATCH",
                "Публикация должна быть привязана к существующей версии Social Post.",
                409,
            )
        approval = await self.session.scalar(
            select(Approval)
            .where(
                Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                Approval.object_id == content_item_id,
                Approval.subject_version == version.version_number,
                Approval.status == ApprovalStatus.APPROVED,
                Approval.subject_snapshot["content_version_id"].as_string()
                == str(content_version_id),
            )
            .order_by(Approval.resolved_at.desc().nullslast())
        )
        if approval is None and item.parent_content_item_id is not None:
            parent_approvals = list(
                (
                    await self.session.scalars(
                        select(Approval)
                        .where(
                            Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                            Approval.object_id == item.parent_content_item_id,
                            Approval.status == ApprovalStatus.APPROVED,
                        )
                        .order_by(Approval.resolved_at.desc().nullslast())
                    )
                ).all()
            )
            for parent_approval in parent_approvals:
                posts = parent_approval.subject_snapshot.get("posts", [])
                if any(
                    str(post.get("content_item_id")) == str(content_item_id)
                    and str(post.get("content_version_id")) == str(content_version_id)
                    for post in posts
                ):
                    approval = parent_approval
                    break
        if approval is None:
            raise AppError(
                "PUBLICATION_APPROVAL_REQUIRED",
                "Для публикации требуется согласованная версия Social Post.",
                409,
            )
        return item

    async def _response(self, publication: Publication) -> PublicationResponse:
        return PublicationResponse(
            id=publication.id,
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            content_version_id=publication.content_version_id,
            channel=publication.channel,
            provider_enabled=(
                settings.telegram_publishing_enabled
                if publication.channel.value == "TELEGRAM"
                else settings.vk_publishing_enabled
            ),
            status=publication.status,
            scheduled_at=publication.scheduled_at,
            approved_for_publish_at=publication.approved_for_publish_at,
            approved_for_publish_by=publication.approved_for_publish_by,
            external_id=publication.external_id,
            external_url=publication.external_url,
            published_at=publication.published_at,
            failure_code=publication.failure_code,
            failure_message=publication.failure_message,
            retry_count=publication.retry_count,
            created_at=publication.created_at,
            updated_at=publication.updated_at,
            provenance=await self._provenance(publication.content_version_id),
        )

    async def create(self, payload: PublicationCreate, user: User) -> PublicationResponse:
        item = await self._ensure_content_version_approved(
            payload.content_item_id, payload.content_version_id, payload.channel
        )
        existing = await self.session.scalar(
            select(Publication)
            .where(
                Publication.content_version_id == payload.content_version_id,
                Publication.channel == payload.channel,
                Publication.status.in_(ACTIVE_PUBLICATION_STATUSES),
            )
            .with_for_update()
        )
        if existing is not None:
            raise AppError(
                "PUBLICATION_ALREADY_EXISTS",
                "Активная публикация для этой версии и канала уже существует.",
                409,
            )
        publication = Publication(
            campaign_id=item.campaign_id,
            content_item_id=item.id,
            content_version_id=payload.content_version_id,
            channel=payload.channel,
            status=PublicationStatus.DRAFT,
        )
        self.session.add(publication)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "PUBLICATION_CREATED",
            operation_key=f"publication-created:{publication.id}",
            campaign_id=item.campaign_id,
            user_id=user.id,
            content_item_id=item.id,
            metadata={
                "publication_id": str(publication.id),
                "content_version_id": str(payload.content_version_id),
            },
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def get(self, publication_id: UUID) -> PublicationResponse:
        publication = await self.session.get(Publication, publication_id)
        if publication is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        return await self._response(publication)

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
        return [await self._response(publication) for publication in publications]

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

    async def _locked(self, publication_id: UUID) -> Publication:
        publication = await self.session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        return publication

    async def approve(self, publication_id: UUID, user: User) -> PublicationResponse:
        publication = await self._locked(publication_id)
        if PublicationStatus.APPROVED not in PUBLICATION_ALLOWED_TRANSITIONS[publication.status]:
            raise AppError(
                "PUBLICATION_INVALID_TRANSITION",
                "Публикацию нельзя согласовать в текущем состоянии.",
                409,
            )
        publication.status = PublicationStatus.APPROVED
        publication.approved_for_publish_at = datetime.now(UTC)
        publication.approved_for_publish_by = user.id
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "PUBLICATION_APPROVED",
            operation_key=f"publication-approved:{publication.id}",
            campaign_id=publication.campaign_id,
            user_id=user.id,
            content_item_id=publication.content_item_id,
            metadata={"publication_id": str(publication.id)},
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def schedule(
        self, publication_id: UUID, scheduled_at: datetime, user: User
    ) -> PublicationResponse:
        if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
            raise AppError(
                "PUBLICATION_TIMEZONE_REQUIRED", "Время публикации должно содержать timezone.", 422
            )
        now = utc_now()
        if scheduled_at <= now:
            raise AppError(
                "PUBLICATION_TIME_IN_PAST", "Нельзя назначить публикацию в прошлом.", 422
            )
        publication = await self._locked(publication_id)
        if PublicationStatus.SCHEDULED not in PUBLICATION_ALLOWED_TRANSITIONS[publication.status]:
            raise AppError(
                "PUBLICATION_INVALID_TRANSITION",
                "Публикацию нельзя назначить в текущем состоянии.",
                409,
            )
        await self._ensure_content_version_approved(
            publication.content_item_id, publication.content_version_id, publication.channel
        )
        previous_scheduled_at = publication.scheduled_at
        event = (
            "PUBLICATION_RESCHEDULED"
            if publication.status is PublicationStatus.SCHEDULED
            else "PUBLICATION_SCHEDULED"
        )
        publication.status = PublicationStatus.SCHEDULED
        publication.scheduled_at = scheduled_at.astimezone(UTC)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            event,
            operation_key=f"publication-schedule:{publication.id}:{scheduled_at.isoformat()}",
            campaign_id=publication.campaign_id,
            user_id=user.id,
            content_item_id=publication.content_item_id,
            metadata={
                "publication_id": str(publication.id),
                **(
                    {"previous_scheduled_at": previous_scheduled_at.isoformat()}
                    if previous_scheduled_at is not None
                    else {}
                ),
                "scheduled_at": publication.scheduled_at.isoformat(),
            },
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def cancel(self, publication_id: UUID, user: User) -> PublicationResponse:
        publication = await self._locked(publication_id)
        if PublicationStatus.CANCELLED not in PUBLICATION_ALLOWED_TRANSITIONS[publication.status]:
            raise AppError("PUBLICATION_INVALID_TRANSITION", "Публикацию уже нельзя отменить.", 409)
        if publication.status is PublicationStatus.CANCELLED:
            return await self._response(publication)
        publication.status = PublicationStatus.CANCELLED
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "PUBLICATION_CANCELLED",
            operation_key=f"publication-cancelled:{publication.id}",
            campaign_id=publication.campaign_id,
            user_id=user.id,
            content_item_id=publication.content_item_id,
            metadata={"publication_id": str(publication.id)},
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def claim_for_publish(
        self, publication_id: UUID, user: User | None = None
    ) -> PublicationResponse:
        """Atomically claim an approved/due publication before Celery execution."""
        publication = await self._locked(publication_id)
        now = utc_now()
        eligible = publication.status is PublicationStatus.APPROVED or (
            publication.status is PublicationStatus.SCHEDULED
            and publication.scheduled_at is not None
            and publication.scheduled_at <= now
        )
        if not eligible:
            raise AppError(
                "PUBLICATION_INVALID_STATE", "Публикацию нельзя запустить в текущем состоянии.", 409
            )
        if publication.channel.value not in {"TELEGRAM", "VK"}:
            raise AppError(
                "PUBLICATION_CHANNEL_UNSUPPORTED",
                "Для этого канала публикация пока недоступна.",
                422,
            )
        if publication.channel.value == "VK" and not settings.vk_publishing_enabled:
            raise AppError("VK_PUBLISHING_DISABLED", "VK publishing отключён.", 409)
        await self._ensure_content_version_approved(
            publication.content_item_id, publication.content_version_id, publication.channel
        )
        publication.status = PublicationStatus.PUBLISHING
        await ActivityLogService(self.session).record(
            "PUBLICATION_QUEUED",
            operation_key=f"publication-queued:{publication.id}",
            campaign_id=publication.campaign_id,
            user_id=user.id if user else None,
            content_item_id=publication.content_item_id,
            metadata={"publication_id": str(publication.id)},
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def execute_vk(
        self, publication_id: UUID, provider: PublicationProvider | None = None
    ) -> PublicationResponse | None:
        publication = await self.session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None or publication.status is not PublicationStatus.PUBLISHING:
            return None
        if publication.channel.value != "VK":
            return None
        if publication.execution_token is not None:
            return None
        publication.execution_token = str(uuid4())
        item = await self.session.scalar(
            select(ContentItem).where(ContentItem.id == publication.content_item_id)
        )
        version = await self.session.scalar(
            select(ContentVersion).where(
                ContentVersion.id == publication.content_version_id,
                ContentVersion.content_item_id == publication.content_item_id,
            )
        )
        if (
            item is None
            or version is None
            or item.status is not ContentStatus.APPROVED
            or item.campaign_id != publication.campaign_id
        ):
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "PUBLICATION_CONTENT_MISSING"
            publication.failure_message = "Согласованная версия материала недоступна."
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        try:
            await self._ensure_content_version_approved(
                publication.content_item_id, publication.content_version_id, publication.channel
            )
        except AppError:
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "PUBLICATION_CONTENT_MISSING"
            publication.failure_message = "Согласованная версия материала недоступна."
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        await ActivityLogService(self.session).record(
            "PUBLICATION_STARTED",
            operation_key=f"publication-started:{publication.id}",
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            metadata={
                "publication_id": str(publication.id),
                "attempt": publication.retry_count + 1,
            },
        )
        await self.session.commit()
        provider = provider or VKProvider()
        try:
            result = await provider.publish(
                text=version.content, chat_id=str(settings.vk_owner_id or "")
            )
        except VKProviderError as error:
            publication = await self.session.scalar(
                select(Publication).where(Publication.id == publication_id).with_for_update()
            )
            if publication is None:
                return None
            publication.status = PublicationStatus.FAILED
            publication.failure_code = (
                "VK_RECONCILIATION_REQUIRED" if error.ambiguous else error.code
            )
            publication.failure_message = (
                "Публикация требует проверки доставки." if error.ambiguous else error.safe_message
            )
            if (
                error.retryable
                and not error.ambiguous
                and publication.retry_count < settings.publication_max_retries
            ):
                publication.retry_count += 1
            event = (
                "PUBLICATION_RECONCILIATION_REQUIRED" if error.ambiguous else "PUBLICATION_FAILED"
            )
            await ActivityLogService(self.session).record(
                event,
                operation_key=f"publication-failed:{publication.id}:{publication.retry_count}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={
                    "publication_id": str(publication.id),
                    "failure_code": publication.failure_code,
                },
            )
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        except Exception:
            publication = await self.session.scalar(
                select(Publication).where(Publication.id == publication_id).with_for_update()
            )
            if publication is None:
                return None
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "VK_RECONCILIATION_REQUIRED"
            publication.failure_message = (
                "Не удалось подтвердить результат доставки; требуется проверка."
            )
            await ActivityLogService(self.session).record(
                "PUBLICATION_RECONCILIATION_REQUIRED",
                operation_key=f"publication-reconciliation:{publication.id}:{publication.retry_count}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={"publication_id": str(publication.id)},
            )
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        publication = await self.session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None:
            return None
        publication.status = PublicationStatus.PUBLISHED
        publication.external_id = result.external_id
        publication.external_url = result.external_url
        publication.published_at = result.published_at
        publication.failure_code = None
        publication.failure_message = None
        await ActivityLogService(self.session).record(
            "PUBLICATION_PUBLISHED",
            operation_key=f"publication-published:{publication.id}",
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            metadata={"publication_id": str(publication.id), "external_id": result.external_id},
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def execute_telegram(
        self, publication_id: UUID, provider: PublicationProvider | None = None
    ) -> PublicationResponse | None:
        """Publish a claimed record; provider errors are persisted without raw payloads."""
        publication = await self.session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None or publication.status is not PublicationStatus.PUBLISHING:
            return None
        if publication.channel.value != "TELEGRAM":
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "PUBLICATION_CHANNEL_UNSUPPORTED"
            publication.failure_message = "Для этого канала Telegram publisher не применяется."
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        if publication.execution_token is not None:
            return None
        publication.execution_token = str(uuid4())
        item = await self.session.scalar(
            select(ContentItem).where(ContentItem.id == publication.content_item_id)
        )
        version = await self.session.scalar(
            select(ContentVersion).where(
                ContentVersion.id == publication.content_version_id,
                ContentVersion.content_item_id == publication.content_item_id,
            )
        )
        if item is None or version is None or item.status is not ContentStatus.APPROVED:
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "PUBLICATION_CONTENT_MISSING"
            publication.failure_message = "Согласованная версия материала недоступна."
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        try:
            await self._ensure_content_version_approved(
                publication.content_item_id,
                publication.content_version_id,
                publication.channel,
            )
        except AppError:
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "PUBLICATION_CONTENT_MISSING"
            publication.failure_message = "Согласованная версия материала недоступна."
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        await ActivityLogService(self.session).record(
            "PUBLICATION_STARTED",
            operation_key=f"publication-started:{publication.id}",
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            metadata={
                "publication_id": str(publication.id),
                "attempt": publication.retry_count + 1,
            },
        )
        await self.session.commit()
        provider = provider or TelegramProvider()
        try:
            result = await provider.publish(
                text=version.content,
                chat_id=settings.telegram_target_chat_id or "",
            )
        except TelegramProviderError as error:
            publication = await self.session.scalar(
                select(Publication).where(Publication.id == publication_id).with_for_update()
            )
            if publication is None:
                return None
            publication.status = PublicationStatus.FAILED
            publication.failure_code = (
                "TELEGRAM_RECONCILIATION_REQUIRED" if error.ambiguous else error.code
            )
            publication.failure_message = (
                "Публикация требует проверки доставки." if error.ambiguous else error.safe_message
            )
            if (
                error.retryable
                and not error.ambiguous
                and publication.retry_count < settings.publication_max_retries
            ):
                publication.retry_count += 1
            event = (
                "PUBLICATION_RECONCILIATION_REQUIRED" if error.ambiguous else "PUBLICATION_FAILED"
            )
            await ActivityLogService(self.session).record(
                event,
                operation_key=f"publication-failed:{publication.id}:{publication.retry_count}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={
                    "publication_id": str(publication.id),
                    "failure_code": publication.failure_code,
                },
            )
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        except Exception:
            publication = await self.session.scalar(
                select(Publication).where(Publication.id == publication_id).with_for_update()
            )
            if publication is None:
                return None
            publication.status = PublicationStatus.FAILED
            publication.failure_code = "TELEGRAM_RECONCILIATION_REQUIRED"
            publication.failure_message = (
                "Не удалось подтвердить результат доставки; требуется проверка."
            )
            await ActivityLogService(self.session).record(
                "PUBLICATION_RECONCILIATION_REQUIRED",
                operation_key=f"publication-reconciliation:{publication.id}:{publication.retry_count}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={"publication_id": str(publication.id)},
            )
            await self.session.commit()
            await self.session.refresh(publication)
            return await self._response(publication)
        publication = await self.session.scalar(
            select(Publication).where(Publication.id == publication_id).with_for_update()
        )
        if publication is None:
            return None
        publication.status = PublicationStatus.PUBLISHED
        publication.external_id = result.external_id
        publication.external_url = result.external_url
        publication.published_at = result.published_at
        publication.failure_code = None
        publication.failure_message = None
        await ActivityLogService(self.session).record(
            "PUBLICATION_PUBLISHED",
            operation_key=f"publication-published:{publication.id}",
            campaign_id=publication.campaign_id,
            content_item_id=publication.content_item_id,
            metadata={"publication_id": str(publication.id), "external_id": result.external_id},
        )
        await self.session.commit()
        await self.session.refresh(publication)
        return await self._response(publication)

    async def recover_stuck_publishing(self, *, cutoff: datetime) -> int:
        """Move abandoned claims to conservative reconciliation state."""
        rows = list(
            (
                await self.session.scalars(
                    select(Publication)
                    .where(
                        Publication.status == PublicationStatus.PUBLISHING,
                        Publication.updated_at < cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for publication in rows:
            publication.status = PublicationStatus.FAILED
            publication.failure_code = (
                "VK_RECONCILIATION_REQUIRED"
                if publication.channel.value == "VK"
                else "TELEGRAM_RECONCILIATION_REQUIRED"
            )
            publication.failure_message = (
                "Состояние доставки неоднозначно; требуется ручная проверка."
            )
            await ActivityLogService(self.session).record(
                "PUBLICATION_RECONCILIATION_REQUIRED",
                operation_key=f"publication-recovery:{publication.id}",
                campaign_id=publication.campaign_id,
                content_item_id=publication.content_item_id,
                metadata={"publication_id": str(publication.id)},
            )
        if rows:
            await self.session.commit()
        return len(rows)
