from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.studio_wordpress import WordPressTarget
from app.models.studio_wordpress import (
    StudioWordPressDelivery,
    StudioWordPressOperation,
    WordPressAction,
    WordPressState,
)
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.services.campaign_studio_service import CampaignStudioService
from app.services.studio_website_package import (
    read_approved_website_article,
    read_website_illustration,
)


class WordPressConfirmation(BaseModel):
    target_url: str = Field(max_length=512)
    target_author_id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")


class WordPressOperationView(BaseModel):
    action: WordPressAction
    status: WordPressState
    remote_id: str | None
    remote_url: str | None
    failure_code: str | None
    reconciliation_pending: bool
    reconciliation_finished_at: datetime | None


class WordPressDeliveryView(BaseModel):
    target_url: str | None = None
    target_author_id: str | None = None
    target_changed: bool = False
    draft_preview_url: str | None = None
    operations: list[WordPressOperationView] = Field(default_factory=list)


def configured_wordpress_target() -> WordPressTarget | None:
    if settings.app_env.lower() not in {"development", "test"}:
        return None
    return WordPressTarget.from_config(
        settings.studio_wordpress_url, settings.studio_wordpress_author_id
    )


class StudioWordPressService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.studio = CampaignStudioService(session)

    async def status(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID
    ) -> WordPressDeliveryView:
        target = configured_wordpress_target()
        if target is None:
            return WordPressDeliveryView()
        await read_approved_website_article(self.studio, user_id, campaign_id, version_id)
        delivery = await self._delivery(campaign_id, version_id, target.url)
        return await self._view(target, delivery)

    async def request_draft(
        self,
        user_id: UUID,
        campaign_id: UUID,
        version_id: UUID,
        confirmation: WordPressConfirmation,
    ) -> WordPressDeliveryView:
        target = self._target(confirmation)
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        article, _ = await read_approved_website_article(
            self.studio, user_id, campaign_id, version_id
        )
        await read_website_illustration(
            self.studio, user_id, campaign_id, article.illustration_version_id
        )
        await self.session.execute(
            insert(StudioWordPressDelivery)
            .values(
                workspace_id=DEFAULT_WORKSPACE_ID,
                campaign_id=campaign_id,
                article_version_id=version_id,
                illustration_version_id=article.illustration_version_id,
                target_url=target.url,
                target_author_id=target.author_id,
            )
            .on_conflict_do_nothing(constraint="uq_studio_wp_article_target")
        )
        delivery = await self._require_delivery(campaign_id, version_id, target)
        await self._enqueue(delivery.id, WordPressAction.DRAFT, user_id)
        return await self._view(target, delivery)

    async def request_publication(
        self,
        user_id: UUID,
        campaign_id: UUID,
        version_id: UUID,
        confirmation: WordPressConfirmation,
    ) -> WordPressDeliveryView:
        target = self._target(confirmation)
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        delivery = await self._require_delivery(campaign_id, version_id, target)
        draft = await self._operation(delivery.id, WordPressAction.DRAFT)
        if draft is None or draft.status != WordPressState.SUCCEEDED:
            raise AppError("WORDPRESS_DRAFT_REQUIRED", "Подтвердите черновик WordPress.", 409)
        if delivery.illustration_version_id is not None:
            await self._enqueue(delivery.id, WordPressAction.IMAGE, user_id)
        await self._enqueue(delivery.id, WordPressAction.PUBLISH, user_id)
        return await self._view(target, delivery)

    async def request_reconciliation(
        self,
        user_id: UUID,
        campaign_id: UUID,
        version_id: UUID,
        action: WordPressAction,
        confirmation: WordPressConfirmation,
    ) -> WordPressDeliveryView:
        target = self._target(confirmation)
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        delivery = await self._require_delivery(campaign_id, version_id, target)
        operation = await self._operation(delivery.id, action, lock=True)
        if operation is None:
            raise AppError("WORDPRESS_OPERATION_NOT_FOUND", "Операция WordPress не найдена.", 404)
        if operation.status == WordPressState.UNKNOWN and (
            operation.reconcile_requested_at is None or operation.reconcile_finished_at is not None
        ):
            operation.reconcile_requested_by = user_id
            operation.reconcile_requested_at = datetime.now(UTC)
            operation.reconcile_started_at = None
            operation.reconcile_finished_at = None
            await self.session.flush()
        return await self._view(target, delivery)

    @staticmethod
    def _target(confirmation: WordPressConfirmation) -> WordPressTarget:
        target = configured_wordpress_target()
        if target is None:
            raise AppError("WORDPRESS_DISABLED", "Тестовый WordPress не подключён.", 404)
        if (confirmation.target_url, confirmation.target_author_id) != (
            target.url,
            str(target.author_id),
        ):
            raise AppError("WORDPRESS_TARGET_CHANGED", "Обновите страницу и проверьте сайт.", 409)
        return target

    async def _require_delivery(
        self, campaign_id: UUID, version_id: UUID, target: WordPressTarget
    ) -> StudioWordPressDelivery:
        delivery = await self._delivery(campaign_id, version_id, target.url, lock=True)
        if delivery is None:
            raise AppError("WORDPRESS_DRAFT_REQUIRED", "Создайте черновик WordPress.", 409)
        if delivery.target_author_id != target.author_id:
            raise AppError("WORDPRESS_TARGET_CHANGED", "Учётная запись WordPress изменилась.", 409)
        return delivery

    async def _delivery(
        self, campaign_id: UUID, version_id: UUID, target_url: str, *, lock: bool = False
    ) -> StudioWordPressDelivery | None:
        stmt = select(StudioWordPressDelivery).where(
            StudioWordPressDelivery.workspace_id == DEFAULT_WORKSPACE_ID,
            StudioWordPressDelivery.campaign_id == campaign_id,
            StudioWordPressDelivery.article_version_id == version_id,
            StudioWordPressDelivery.target_url == target_url,
        )
        result = await self.session.execute(stmt.with_for_update() if lock else stmt)
        return result.scalar_one_or_none()

    async def _operation(
        self, delivery_id: UUID, action: WordPressAction, *, lock: bool = False
    ) -> StudioWordPressOperation | None:
        stmt = select(StudioWordPressOperation).where(
            StudioWordPressOperation.delivery_id == delivery_id,
            StudioWordPressOperation.action == action,
        )
        result = await self.session.execute(stmt.with_for_update() if lock else stmt)
        return result.scalar_one_or_none()

    async def _enqueue(self, delivery_id: UUID, action: WordPressAction, user_id: UUID) -> None:
        await self.session.execute(
            insert(StudioWordPressOperation)
            .values(
                delivery_id=delivery_id,
                action=action,
                status=WordPressState.QUEUED,
                requested_by=user_id,
            )
            .on_conflict_do_nothing(constraint="uq_studio_wp_delivery_action")
        )

    async def _view(
        self, target: WordPressTarget, delivery: StudioWordPressDelivery | None
    ) -> WordPressDeliveryView:
        operations = (
            (
                await self.session.scalars(
                    select(StudioWordPressOperation)
                    .where(StudioWordPressOperation.delivery_id == delivery.id)
                    .order_by(StudioWordPressOperation.action)
                )
            ).all()
            if delivery
            else ()
        )
        target_changed = delivery is not None and delivery.target_author_id != target.author_id
        draft = next((op for op in operations if op.action == WordPressAction.DRAFT), None)
        draft_preview_url = None
        if (
            not target_changed
            and draft is not None
            and draft.status == WordPressState.SUCCEEDED
            and draft.remote_id is not None
            and all(op.action != WordPressAction.PUBLISH for op in operations)
        ):
            draft_preview_url = f"{target.url}/?p={draft.remote_id}&preview=true"
        return WordPressDeliveryView(
            target_url=target.url,
            target_author_id=str(target.author_id),
            target_changed=target_changed,
            draft_preview_url=draft_preview_url,
            operations=[
                WordPressOperationView(
                    action=WordPressAction(op.action),
                    status=WordPressState(op.status),
                    remote_id=str(op.remote_id) if op.remote_id is not None else None,
                    remote_url=op.remote_url,
                    failure_code=op.failure_code,
                    reconciliation_pending=(
                        op.reconcile_requested_at is not None and op.reconcile_finished_at is None
                    ),
                    reconciliation_finished_at=op.reconcile_finished_at,
                )
                for op in operations
            ],
        )
