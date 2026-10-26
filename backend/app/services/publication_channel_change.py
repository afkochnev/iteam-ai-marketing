"""Prepare an independently approved adaptation; explicitly replace one pending delivery."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.activity import ActivityLog
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentVersion,
    ContentVersionSource,
)
from app.models.publication import ACTIVE_PUBLICATION_STATUSES, Publication, PublicationStatus
from app.models.publication_plan import PublicationPlan, PublicationPlanStatus
from app.models.user import User
from app.schemas.publication_channel_change import (
    ChannelChangeConfirm,
    ChannelChangePrepare,
    ChannelChangePrepared,
)
from app.services.activity_log_service import ActivityLogService
from app.services.approval_service import ApprovalService
from app.services.publication_service import PublicationService
from app.services.social_content_quality import (
    social_text_quality_errors,
    social_text_quality_message,
)


class PublicationChannelChangeService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def pending(self, publication_id: UUID) -> ChannelChangePrepared | None:
        source = await self.session.get(Publication, publication_id)
        if source is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        target = "TELEGRAM" if source.channel.value == "VK" else "VK"
        event = await self.session.scalar(
            select(ActivityLog).where(
                ActivityLog.operation_key == f"channel-change-prepared:{source.id}:{target}"
            )
        )
        if event is None:
            return None
        item = await self.session.get(ContentItem, event.content_item_id)
        if (
            item is None
            or item.current_version_id is None
            or item.channel is None
            or (item.metadata_ or {}).get("channel_change_applied")
        ):
            return None
        return ChannelChangePrepared(
            content_item_id=item.id,
            content_version_id=item.current_version_id,
            channel=item.channel,
        )

    async def _locked(self, publication_id: UUID) -> Publication:
        source = await self.session.get(Publication, publication_id)
        if source is None:
            raise AppError("PUBLICATION_NOT_FOUND", "Публикация не найдена.", 404)
        campaign = await self.session.get(
            Campaign, source.campaign_id, with_for_update=True, populate_existing=True
        )
        if campaign is None or campaign.status is CampaignStatus.ARCHIVED:
            raise AppError("CAMPAIGN_ARCHIVED", "Кампания находится в архиве.", 409)
        (
            await self.session.scalars(
                select(ContentItem)
                .where(ContentItem.campaign_id == source.campaign_id)
                .order_by(ContentItem.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        source = await self.session.get(
            Publication, publication_id, with_for_update=True, populate_existing=True
        )
        assert source is not None
        return source

    @staticmethod
    def _pending(source: Publication) -> None:
        if (
            source.status not in {PublicationStatus.SCHEDULED, PublicationStatus.APPROVED}
            or source.execution_token is not None
            or source.failure_code
            in {"VK_RECONCILIATION_REQUIRED", "TELEGRAM_RECONCILIATION_REQUIRED"}
        ):
            raise AppError(
                "CHANNEL_CHANGE_BLOCKED",
                "Отправка уже выполняется, завершена или требует проверки результата.",
                409,
            )

    async def prepare(
        self, publication_id: UUID, payload: ChannelChangePrepare, user: User
    ) -> ChannelChangePrepared:
        source = await self._locked(publication_id)
        key = f"channel-change-prepared:{source.id}:{payload.channel.value}"
        existing = await self.session.scalar(
            select(ActivityLog).where(ActivityLog.operation_key == key)
        )
        if existing is not None:
            item = await self.session.get(ContentItem, existing.content_item_id)
            if item is None or item.current_version_id is None:
                raise AppError("CHANNEL_CHANGE_BLOCKED", "Подготовленный материал недоступен.", 409)
            existing_version = await self.session.get(ContentVersion, item.current_version_id)
            if (
                existing_version is None
                or existing_version.content != payload.content
                or (item.metadata_ or {}).get("channel_change_source_version_id")
                != str(payload.expected_content_version_id)
            ):
                raise AppError(
                    "CHANNEL_CHANGE_ALREADY_PREPARED",
                    "Адаптация уже существует. Откройте её для редактирования.",
                    409,
                )
            return ChannelChangePrepared(
                content_item_id=item.id,
                content_version_id=item.current_version_id,
                channel=payload.channel,
            )
        self._pending(source)
        if source.content_version_id != payload.expected_content_version_id:
            raise AppError("CONTENT_VERSION_CONFLICT", "Версия исходной отправки изменилась.", 409)
        if source.channel is payload.channel:
            raise AppError("CHANNEL_UNCHANGED", "Выберите другой канал.", 422)
        errors = social_text_quality_errors(payload.content)
        if errors:
            raise AppError("SOCIAL_POST_QUALITY_INVALID", social_text_quality_message(errors), 422)
        service = PublicationService(self.session)
        original = await service._ensure_content_version_approved(
            source.content_item_id, source.content_version_id, source.channel
        )
        previous = await self.session.get(ContentVersion, source.content_version_id)
        assert previous is not None
        metadata = {
            "channel_change_source_publication_id": str(source.id),
            "channel_change_source_version_id": str(source.content_version_id),
            "channel_change_source_updated_at": source.updated_at.astimezone(UTC).isoformat(),
            "channel_change_applied": False,
        }
        plan_item = await service._plan_item(original)
        if plan_item is not None:
            metadata["channel_change_origin_plan_item_id"] = str(plan_item.id)
        item = ContentItem(
            campaign_id=source.campaign_id,
            source_task_id=original.source_task_id,
            content_type=original.content_type,
            title=original.title,
            status=ContentStatus.WAITING_APPROVAL,
            author_agent_id=original.author_agent_id,
            channel=payload.channel,
            metadata_=metadata,
        )
        self.session.add(item)
        await self.session.flush()
        structured = deepcopy(previous.structured_content)
        structured.update(text_markdown=payload.content, channel=payload.channel.value)
        version = ContentVersion(
            content_item_id=item.id,
            version_number=1,
            content=payload.content,
            structured_content=structured,
            created_by_user_id=user.id,
            change_description=f"Адаптация для {payload.channel.value}",
        )
        self.session.add(version)
        await self.session.flush()
        for reference in await self.session.scalars(
            select(ContentVersionSource).where(
                ContentVersionSource.content_version_id == previous.id
            )
        ):
            self.session.add(
                ContentVersionSource(
                    content_version_id=version.id,
                    knowledge_pack_item_id=reference.knowledge_pack_item_id,
                    section_key=reference.section_key,
                    position=reference.position,
                )
            )
        for origin in await self.session.scalars(
            select(ContentDerivation).where(
                ContentDerivation.derived_content_version_id == previous.id
            )
        ):
            self.session.add(
                ContentDerivation(
                    derived_content_version_id=version.id,
                    source_content_version_id=origin.source_content_version_id,
                    source_section_key="channel_change_source_article"
                    if origin.source_section_key == "publication_plan_item"
                    else origin.source_section_key,
                )
            )
        item.current_version_id = version.id
        await ApprovalService(self.session).create_content_approval(
            item.id,
            1,
            {
                "content_item_id": str(item.id),
                "content_version_id": str(version.id),
                "version_number": 1,
                "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
            },
            None,
        )
        await ActivityLogService(self.session).record(
            "PUBLICATION_CHANNEL_CHANGE_PREPARED",
            operation_key=key,
            campaign_id=source.campaign_id,
            user_id=user.id,
            content_item_id=item.id,
            metadata={
                "source_publication_id": str(source.id),
                "channel": payload.channel.value,
                "content_version_id": str(version.id),
            },
        )
        await self.session.commit()
        return ChannelChangePrepared(
            content_item_id=item.id, content_version_id=version.id, channel=payload.channel
        )

    async def confirm(
        self, publication_id: UUID, payload: ChannelChangeConfirm, user: User
    ) -> UUID:
        source = await self._locked(publication_id)
        key = f"channel-change-confirmed:{payload.content_item_id}"
        existing = await self.session.scalar(
            select(ActivityLog).where(ActivityLog.operation_key == key)
        )
        if existing is not None:
            if (
                existing.metadata_.get("source_publication_id") != str(source.id)
                or existing.metadata_.get("content_version_id")
                != str(payload.expected_content_version_id)
                or existing.metadata_.get("scheduled_at")
                != payload.scheduled_at.astimezone(UTC).isoformat()
            ):
                raise AppError(
                    "CHANNEL_CHANGE_ALREADY_APPLIED",
                    "Замена уже выполнена с другими параметрами.",
                    409,
                )
            return UUID(existing.metadata_["publication_id"])
        self._pending(source)
        item = await self.session.get(ContentItem, payload.content_item_id)
        if item is None or item.channel is None or item.campaign_id != source.campaign_id:
            raise AppError("CONTENT_NOT_FOUND", "Подготовленный пост не найден.", 404)
        metadata = item.metadata_ or {}
        if (
            metadata.get("channel_change_source_publication_id") != str(source.id)
            or metadata.get("channel_change_source_version_id") != str(source.content_version_id)
            or metadata.get("channel_change_source_updated_at")
            != source.updated_at.astimezone(UTC).isoformat()
            or item.current_version_id != payload.expected_content_version_id
            or item.channel is source.channel
        ):
            raise AppError(
                "CHANNEL_CHANGE_STALE",
                "Публикация или текст изменились. Проверьте замену заново.",
                409,
            )
        now = datetime.now(UTC)
        if payload.scheduled_at <= now:
            raise AppError("PUBLICATION_TIME_IN_PAST", "Выберите будущую дату.", 422)
        service = PublicationService(self.session)
        original = await service._ensure_content_version_approved(
            source.content_item_id, source.content_version_id, source.channel
        )
        plan_item = await service._plan_item(original)
        if plan_item is not None:
            current = await self.session.scalar(
                select(PublicationPlan)
                .where(
                    PublicationPlan.campaign_id == source.campaign_id,
                    PublicationPlan.status == PublicationPlanStatus.APPROVED,
                )
                .order_by(PublicationPlan.created_at.desc(), PublicationPlan.id.desc())
                .limit(1)
            )
            if current is None or current.id != plan_item.publication_plan_id:
                raise AppError(
                    "PUBLICATION_PLAN_NOT_CURRENT",
                    "Медиаплан изменился. Проверьте замену заново.",
                    409,
                )

        await service._ensure_content_version_approved(
            item.id,
            payload.expected_content_version_id,
            item.channel,
            require_integrity_hash=True,
            allow_pending_channel_change=True,
        )
        duplicate = await self.session.scalar(
            select(Publication.id)
            .where(
                Publication.content_item_id == item.id,
                Publication.status != PublicationStatus.CANCELLED,
            )
            .limit(1)
        )
        if duplicate is not None:
            raise AppError(
                "PUBLICATION_ALREADY_EXISTS", "Для нового текста уже есть отправка.", 409
            )
        collision = await self.session.scalar(
            select(Publication.id)
            .where(
                Publication.campaign_id == source.campaign_id,
                Publication.id != source.id,
                Publication.channel == item.channel,
                Publication.scheduled_at == payload.scheduled_at,
                Publication.status.in_(ACTIVE_PUBLICATION_STATUSES),
            )
            .limit(1)
        )
        if collision is not None:
            raise AppError("PLAN_DUPLICATE_SLOT", "В выбранном канале уже занят этот слот.", 409)
        source.status = PublicationStatus.CANCELLED
        item.metadata_ = {**metadata, "channel_change_applied": True}
        await self.session.flush()
        replacement = Publication(
            campaign_id=source.campaign_id,
            content_item_id=item.id,
            content_version_id=payload.expected_content_version_id,
            channel=item.channel,
            status=PublicationStatus.SCHEDULED,
            scheduled_at=payload.scheduled_at.astimezone(UTC),
            approved_for_publish_at=now,
            approved_for_publish_by=user.id,
        )
        self.session.add(replacement)
        await self.session.flush()
        await ActivityLogService(self.session).record(
            "PUBLICATION_CANCELLED",
            operation_key=f"publication-cancelled:{source.id}",
            campaign_id=source.campaign_id,
            user_id=user.id,
            content_item_id=source.content_item_id,
            metadata={"publication_id": str(source.id)},
        )
        await ActivityLogService(self.session).record(
            "PUBLICATION_CHANNEL_CHANGE_CONFIRMED",
            operation_key=key,
            campaign_id=source.campaign_id,
            user_id=user.id,
            content_item_id=item.id,
            metadata={
                "source_publication_id": str(source.id),
                "publication_id": str(replacement.id),
                "content_version_id": str(payload.expected_content_version_id),
                "scheduled_at": payload.scheduled_at.astimezone(UTC).isoformat(),
                "channel": item.channel.value,
            },
        )
        await self.session.commit()
        return replacement.id
