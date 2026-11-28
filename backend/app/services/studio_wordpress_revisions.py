from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.errors import AppError
from app.integrations.studio_wordpress import WordPressTarget
from app.models.content_asset import ContentAssetApproval, ContentAssetVersion
from app.models.studio_wordpress import (
    StudioWordPressDelivery,
    StudioWordPressRevision,
    WordPressAction,
    WordPressState,
)
from app.schemas.content_asset import ContentAssetVersionResponse
from app.services.studio_website_package import (
    read_approved_website_article,
    read_website_illustration,
)
from app.services.studio_wordpress import (
    StudioWordPressService,
    WordPressConfirmation,
    configured_wordpress_target,
)

RevisionMode = Literal["update", "restore"]
RevisionPhase = Literal["image", "replace"]
ACTIVE_STATES = (WordPressState.QUEUED, WordPressState.RUNNING, WordPressState.UNKNOWN)


class WordPressRevisionConfirmation(WordPressConfirmation):
    command_id: UUID
    expected_revision_id: UUID | None
    article_version_id: UUID
    mode: RevisionMode


class WordPressRevisionOption(BaseModel):
    id: UUID
    version_number: int
    title: str
    mode: RevisionMode


class WordPressRevisionView(BaseModel):
    id: UUID
    article_version_id: UUID
    version_number: int
    title: str
    mode: RevisionMode
    phase: RevisionPhase
    status: WordPressState
    requested_at: datetime
    failure_code: str | None
    reconciliation_pending: bool
    reconciliation_finished_at: datetime | None
    reconciliation_failure_code: str | None


class WordPressRevisionsView(BaseModel):
    current_revision_id: UUID | None
    current_version_id: UUID
    versions: list[WordPressRevisionOption]
    revisions: list[WordPressRevisionView]


class StudioWordPressRevisionService(StudioWordPressService):
    async def revision_status(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID
    ) -> WordPressRevisionsView:
        target = configured_wordpress_target()
        if target is None:
            raise AppError("WORDPRESS_DISABLED", "Тестовый WordPress не подключён.", 404)
        delivery, original = await self._published_delivery(
            user_id, campaign_id, version_id, target, lock=False
        )
        return await self._revisions_view(delivery, original.asset_id)

    async def request_revision(
        self,
        user_id: UUID,
        campaign_id: UUID,
        version_id: UUID,
        confirmation: WordPressRevisionConfirmation,
    ) -> WordPressRevisionsView:
        target = self._target(confirmation)
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        delivery, original = await self._published_delivery(
            user_id, campaign_id, version_id, target, lock=True
        )
        existing = await self.session.get(StudioWordPressRevision, confirmation.command_id)
        if existing is not None:
            self._check_existing(existing, delivery.id, user_id, confirmation)
            return await self._revisions_view(delivery, original.asset_id)

        history = await self._history(delivery.id)
        current = self._current_revision(history)
        if confirmation.expected_revision_id != (current.id if current else None):
            raise AppError("WORDPRESS_REVISION_STALE", "Обновите историю публикации.", 409)
        if any(revision.status in ACTIVE_STATES for revision in history):
            raise AppError(
                "WORDPRESS_REVISION_ACTIVE",
                "Завершите текущую операцию WordPress или сверьте её результат.",
                409,
            )

        approved, _ = await read_approved_website_article(
            self.studio, user_id, campaign_id, confirmation.article_version_id
        )
        if approved.asset_id != original.asset_id:
            raise AppError("WORDPRESS_REVISION_ASSET", "Выберите версию этой статьи.", 409)
        current_version_id = current.article_version_id if current else original.id
        published = self._published_versions(original.id, history)
        expected_mode = "restore" if approved.id in published else "update"
        if approved.id == current_version_id or confirmation.mode != expected_mode:
            raise AppError("WORDPRESS_REVISION_STALE", "Обновите историю публикации.", 409)

        visual = await read_website_illustration(
            self.studio, user_id, campaign_id, approved.illustration_version_id
        )
        media_id, media_slug = await self._existing_media(delivery, approved)
        phase = "image" if visual is not None and media_id is None else "replace"
        created = await self.session.scalar(
            insert(StudioWordPressRevision)
            .values(
                id=confirmation.command_id,
                delivery_id=delivery.id,
                expected_revision_id=confirmation.expected_revision_id,
                article_version_id=approved.id,
                mode=confirmation.mode,
                phase=phase,
                status=WordPressState.QUEUED,
                requested_by=user_id,
                media_id=media_id,
                media_slug=media_slug,
            )
            .on_conflict_do_nothing(index_elements=[StudioWordPressRevision.id])
            .returning(StudioWordPressRevision.id)
        )
        if created is None:
            existing = await self.session.get(StudioWordPressRevision, confirmation.command_id)
            self._check_existing(existing, delivery.id, user_id, confirmation)
        return await self._revisions_view(delivery, original.asset_id)

    async def request_revision_reconciliation(
        self,
        user_id: UUID,
        campaign_id: UUID,
        version_id: UUID,
        command_id: UUID,
        confirmation: WordPressConfirmation,
    ) -> WordPressRevisionsView:
        target = self._target(confirmation)
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        delivery, original = await self._published_delivery(
            user_id, campaign_id, version_id, target, lock=True
        )
        revision = await self.session.scalar(
            select(StudioWordPressRevision)
            .where(
                StudioWordPressRevision.id == command_id,
                StudioWordPressRevision.delivery_id == delivery.id,
            )
            .with_for_update()
        )
        if revision is None:
            raise AppError("WORDPRESS_OPERATION_NOT_FOUND", "Операция WordPress не найдена.", 404)
        if revision.status == WordPressState.UNKNOWN and (
            revision.reconcile_requested_at is None or revision.reconcile_finished_at is not None
        ):
            revision.reconcile_requested_by = user_id
            revision.reconcile_requested_at = datetime.now(UTC)
            revision.reconcile_started_at = None
            revision.reconcile_finished_at = None
            revision.reconciliation_failure_code = None
            await self.session.flush()
        return await self._revisions_view(delivery, original.asset_id)

    async def _published_delivery(
        self,
        user_id: UUID,
        campaign_id: UUID,
        version_id: UUID,
        target: WordPressTarget,
        *,
        lock: bool,
    ) -> tuple[StudioWordPressDelivery, ContentAssetVersionResponse]:
        original, _ = await read_approved_website_article(
            self.studio, user_id, campaign_id, version_id
        )
        delivery = await self._delivery(campaign_id, version_id, target.url, lock=lock)
        if delivery is None:
            raise AppError("WORDPRESS_PUBLICATION_REQUIRED", "Опубликуйте статью в WordPress.", 409)
        if delivery.target_author_id != target.author_id:
            raise AppError("WORDPRESS_TARGET_CHANGED", "Учётная запись WordPress изменилась.", 409)
        receipt = await self._operation(delivery.id, WordPressAction.PUBLISH)
        if (
            receipt is None
            or receipt.status != WordPressState.SUCCEEDED
            or receipt.remote_id is None
            or receipt.remote_url is None
        ):
            raise AppError(
                "WORDPRESS_PUBLICATION_REQUIRED",
                "Подтвердите публикацию WordPress.",
                409,
            )
        return delivery, original

    async def _history(self, delivery_id: UUID) -> list[StudioWordPressRevision]:
        return list(
            await self.session.scalars(
                select(StudioWordPressRevision)
                .where(StudioWordPressRevision.delivery_id == delivery_id)
                .order_by(
                    StudioWordPressRevision.requested_at.desc(),
                    StudioWordPressRevision.id.desc(),
                )
            )
        )

    @staticmethod
    def _current_revision(
        history: list[StudioWordPressRevision],
    ) -> StudioWordPressRevision | None:
        succeeded = [
            revision for revision in history if revision.status == WordPressState.SUCCEEDED
        ]
        parents = {revision.expected_revision_id for revision in succeeded}
        return next((revision for revision in succeeded if revision.id not in parents), None)

    @staticmethod
    def _published_versions(original_id: UUID, history: list[StudioWordPressRevision]) -> set[UUID]:
        return {
            original_id,
            *(
                revision.article_version_id
                for revision in history
                if revision.status == WordPressState.SUCCEEDED
            ),
        }

    @staticmethod
    def _check_existing(
        existing: StudioWordPressRevision | None,
        delivery_id: UUID,
        user_id: UUID,
        confirmation: WordPressRevisionConfirmation,
    ) -> None:
        if existing is None or (
            existing.delivery_id,
            existing.requested_by,
            existing.expected_revision_id,
            existing.article_version_id,
            existing.mode,
        ) != (
            delivery_id,
            user_id,
            confirmation.expected_revision_id,
            confirmation.article_version_id,
            confirmation.mode,
        ):
            raise AppError("WORDPRESS_REVISION_CONFLICT", "Подтвердите версию ещё раз.", 409)

    async def _existing_media(
        self,
        delivery: StudioWordPressDelivery,
        approved: ContentAssetVersionResponse,
    ) -> tuple[int | None, str | None]:
        illustration = approved.illustration_version_id
        if illustration is None:
            return None, None
        if illustration == delivery.illustration_version_id:
            image = await self._operation(delivery.id, WordPressAction.IMAGE)
            if image is not None and image.status == WordPressState.SUCCEEDED and image.remote_id:
                return image.remote_id, f"iteam-{delivery.id.hex}-image"

        reusable = await self.session.execute(
            select(StudioWordPressRevision.media_id, StudioWordPressRevision.media_slug)
            .join(
                ContentAssetVersion,
                ContentAssetVersion.id == StudioWordPressRevision.article_version_id,
            )
            .where(
                StudioWordPressRevision.delivery_id == delivery.id,
                StudioWordPressRevision.status == WordPressState.SUCCEEDED,
                ContentAssetVersion.illustration_version_id == illustration,
                StudioWordPressRevision.media_id.is_not(None),
            )
            .order_by(
                StudioWordPressRevision.requested_at.desc(),
                StudioWordPressRevision.id.desc(),
            )
            .limit(1)
        )
        match = reusable.first()
        return (match.media_id, match.media_slug) if match else (None, None)

    async def _revisions_view(
        self, delivery: StudioWordPressDelivery, asset_id: UUID
    ) -> WordPressRevisionsView:
        history = await self._history(delivery.id)
        current = self._current_revision(history)
        current_version_id = current.article_version_id if current else delivery.article_version_id
        published = self._published_versions(delivery.article_version_id, history)

        rows = await self.session.execute(
            select(
                ContentAssetVersion.id,
                ContentAssetVersion.version_number,
                ContentAssetVersion.payload["title"].astext.label("title"),
            )
            .join(
                ContentAssetApproval,
                ContentAssetApproval.version_id == ContentAssetVersion.id,
            )
            .where(ContentAssetVersion.asset_id == asset_id)
            .order_by(ContentAssetVersion.version_number.desc())
        )
        options = {
            row.id: WordPressRevisionOption(
                id=row.id,
                version_number=row.version_number,
                title=row.title,
                mode="restore" if row.id in published else "update",
            )
            for row in rows
        }
        revisions = [
            WordPressRevisionView(
                id=revision.id,
                article_version_id=revision.article_version_id,
                version_number=options[revision.article_version_id].version_number,
                title=options[revision.article_version_id].title,
                mode=cast(RevisionMode, revision.mode),
                phase=cast(RevisionPhase, revision.phase),
                status=WordPressState(revision.status),
                requested_at=revision.requested_at,
                failure_code=revision.failure_code,
                reconciliation_pending=(
                    revision.reconcile_requested_at is not None
                    and revision.reconcile_finished_at is None
                ),
                reconciliation_finished_at=revision.reconcile_finished_at,
                reconciliation_failure_code=revision.reconciliation_failure_code,
            )
            for revision in history
        ]
        return WordPressRevisionsView(
            current_revision_id=current.id if current else None,
            current_version_id=current_version_id,
            versions=[option for option in options.values() if option.id != current_version_id],
            revisions=revisions,
        )
