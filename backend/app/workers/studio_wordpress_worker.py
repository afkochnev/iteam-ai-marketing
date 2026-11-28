import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.database import async_session_factory
from app.core.errors import AppError
from app.integrations.studio_wordpress import (
    WordPressArticle,
    WordPressError,
    WordPressImage,
    WordPressPost,
    WordPressStudioClient,
    WordPressTarget,
)
from app.models.studio_wordpress import (
    StudioWordPressDelivery,
    StudioWordPressOperation,
    WordPressAction,
    WordPressState,
)
from app.schemas.content_asset import CreativeMediaObject
from app.services.campaign_studio_service import CampaignStudioService
from app.services.studio_website_package import (
    read_approved_website_article,
    read_website_illustration,
    website_image_bytes,
)
from app.services.studio_wordpress import configured_wordpress_target

logger = logging.getLogger(__name__)
LEASE_DURATION = timedelta(minutes=2)
OPERATION_TIMEOUT = 60


@dataclass(frozen=True)
class Claim:
    operation_id: UUID
    lease_id: UUID
    reconciliation: bool


@dataclass(frozen=True)
class Work:
    action: WordPressAction
    article: WordPressArticle
    image: WordPressImage | None
    local_media: CreativeMediaObject | None
    post_id: int | None
    media_id: int


async def _claim(session: AsyncSession, target: WordPressTarget) -> Claim | None:
    operation = StudioWordPressOperation
    delivery = StudioWordPressDelivery
    now = datetime.now(UTC)
    stale_before = now - LEASE_DURATION
    target_filter = (
        delivery.target_url == target.url,
        delivery.target_author_id == target.author_id,
    )
    await session.execute(
        update(operation)
        .where(
            operation.delivery_id.in_(select(delivery.id).where(*target_filter)),
            operation.status == WordPressState.RUNNING,
            operation.started_at <= stale_before,
        )
        .values(
            status=WordPressState.UNKNOWN,
            finished_at=now,
            failure_code="WORDPRESS_WORKER_LOST",
            lease_id=None,
        )
    )
    image_command = aliased(operation)
    image_ready = (
        select(image_command.id)
        .where(
            image_command.delivery_id == delivery.id,
            image_command.action == WordPressAction.IMAGE,
            image_command.status == WordPressState.SUCCEEDED,
        )
        .exists()
    )
    candidate = await session.scalar(
        select(operation)
        .join(delivery, delivery.id == operation.delivery_id)
        .where(
            *target_filter,
            or_(
                operation.action != WordPressAction.PUBLISH,
                delivery.illustration_version_id.is_(None),
                image_ready,
            ),
            or_(
                and_(
                    operation.status == WordPressState.QUEUED,
                    or_(operation.scheduled_for.is_(None), operation.scheduled_for <= now),
                ),
                and_(
                    operation.status == WordPressState.UNKNOWN,
                    operation.reconcile_requested_at.is_not(None),
                    operation.reconcile_finished_at.is_(None),
                    or_(
                        operation.reconcile_started_at.is_(None),
                        operation.reconcile_started_at <= stale_before,
                    ),
                ),
            ),
        )
        .order_by(operation.requested_at, operation.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=operation)
    )
    if candidate is None:
        return None
    token = uuid4()
    candidate.lease_id = token
    reconciliation = candidate.status == WordPressState.UNKNOWN
    if reconciliation:
        candidate.reconcile_started_at = now
    else:
        candidate.status = WordPressState.RUNNING
        candidate.started_at = now
    return Claim(candidate.id, token, reconciliation)


async def _load_work(session: AsyncSession, claim: Claim, target: WordPressTarget) -> Work:
    operation = await session.get(StudioWordPressOperation, claim.operation_id)
    if operation is None or operation.lease_id != claim.lease_id:
        raise WordPressError("WORDPRESS_CLAIM_LOST")
    delivery = await session.get(StudioWordPressDelivery, operation.delivery_id)
    if delivery is None or (delivery.target_url, delivery.target_author_id) != (
        target.url,
        target.author_id,
    ):
        raise WordPressError("WORDPRESS_TARGET_CHANGED")
    user = operation.reconcile_requested_by if claim.reconciliation else operation.requested_by
    if user is None:
        raise WordPressError("WORDPRESS_APPROVAL_NOT_FOUND")
    studio = CampaignStudioService(session)
    await studio.require_campaign_access(user, delivery.campaign_id, write=True)
    version, article = await read_approved_website_article(
        studio, user, delivery.campaign_id, delivery.article_version_id
    )
    if version.illustration_version_id != delivery.illustration_version_id:
        raise WordPressError("WORDPRESS_APPROVED_VERSION_MISMATCH")
    slug = f"iteam-{delivery.id.hex}"
    html = "\n".join(
        f"<p>{escape(line, quote=False)}</p>" for line in article.body.splitlines() if line
    )
    payload = WordPressArticle(slug, article.title, html)
    action = WordPressAction(operation.action)
    if action == WordPressAction.DRAFT:
        return Work(action, payload, None, None, None, 0)

    previous = await session.scalars(
        select(StudioWordPressOperation).where(
            StudioWordPressOperation.delivery_id == delivery.id,
            StudioWordPressOperation.status == WordPressState.SUCCEEDED,
        )
    )
    receipts = {item.action: item.remote_id for item in previous}
    post_id = receipts.get(WordPressAction.DRAFT)
    if post_id is None:
        raise WordPressError("WORDPRESS_DRAFT_REQUIRED")
    visual = await read_website_illustration(
        studio, user, delivery.campaign_id, delivery.illustration_version_id
    )
    if visual is None:
        if action == WordPressAction.IMAGE:
            raise WordPressError("WORDPRESS_APPROVED_VERSION_MISMATCH")
        return Work(action, payload, None, None, post_id, 0)
    media = visual.media
    image = WordPressImage(f"{slug}-image", media.sha256, media.size_bytes, visual.details.alt_text)
    media_id = receipts.get(WordPressAction.IMAGE) or 0
    if action == WordPressAction.PUBLISH and not media_id:
        raise WordPressError("WORDPRESS_MEDIA_REQUIRED")
    return Work(action, payload, image, media, post_id, media_id)


async def _execute(
    adapter: WordPressStudioClient, work: Work, *, reconciliation: bool
) -> WordPressPost | None:
    await adapter.verify_actor()
    if work.action == WordPressAction.DRAFT:
        if reconciliation:
            return await adapter.find_draft(work.article)
        return await adapter.create_draft(work.article)
    if work.post_id is None:
        raise WordPressError("WORDPRESS_DRAFT_REQUIRED")
    if work.action == WordPressAction.IMAGE:
        if work.image is None or work.local_media is None:
            raise WordPressError("WORDPRESS_MEDIA_REQUIRED")
        if reconciliation:
            media_id = await adapter.find_image(work.image, work.post_id)
            return WordPressPost(media_id) if media_id is not None else None
        await adapter.read_draft(work.article, work.post_id)
        content = await website_image_bytes(work.local_media)
        return WordPressPost(await adapter.upload_image(work.image, work.post_id, content))
    if work.image is not None:
        await adapter.verify_image(work.image, work.post_id, work.media_id)
    if reconciliation:
        return await adapter.find_publication(work.article, work.post_id, work.media_id)
    await adapter.read_draft(work.article, work.post_id)
    return await adapter.publish(work.article, work.post_id, work.media_id)


async def run_next_wordpress_operation(
    sessions: async_sessionmaker[AsyncSession], adapter: WordPressStudioClient
) -> bool:
    async with sessions.begin() as session:
        claim = await _claim(session, adapter.target)
    if claim is None:
        return False

    receipt = None
    failure = None
    adapter.write_attempted = False
    try:
        async with asyncio.timeout(OPERATION_TIMEOUT):
            async with sessions() as session:
                work = await _load_work(session, claim, adapter.target)
            receipt = await _execute(adapter, work, reconciliation=claim.reconciliation)
    except (WordPressError, AppError) as error:
        failure = error.code
    except TimeoutError:
        failure = "WORDPRESS_OPERATION_TIMEOUT"
    except Exception:
        failure = "WORDPRESS_OPERATION_ERROR"
        logger.error("Unexpected WordPress operation failure for %s", claim.operation_id)

    async with sessions.begin() as session:
        operation = await session.get(
            StudioWordPressOperation, claim.operation_id, with_for_update=True
        )
        if operation is not None and operation.lease_id == claim.lease_id:
            operation.lease_id = None
            now = datetime.now(UTC)
            if claim.reconciliation:
                operation.reconcile_finished_at = now
                operation.reconciliation_failure_code = failure
            else:
                operation.finished_at = now
                operation.failure_code = failure
            if receipt is not None:
                operation.status = WordPressState.SUCCEEDED
                operation.remote_id = receipt.id
                operation.remote_url = receipt.url
            elif not claim.reconciliation:
                operation.status = (
                    WordPressState.UNKNOWN if adapter.write_attempted else WordPressState.FAILED
                )
    return True


def _credentials() -> tuple[str, str]:
    try:
        path = Path(os.environ["STUDIO_WORDPRESS_CREDENTIALS_FILE"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        username = payload["username"]
        password = payload["application_password"]
        if not isinstance(username, str) or not username.strip() or ":" in username:
            raise ValueError
        if not isinstance(password, str) or not password.strip():
            raise ValueError
    except (KeyError, TypeError, ValueError, OSError):
        raise SystemExit("WordPress worker credentials are missing or invalid.") from None
    return username, password


async def _main() -> None:
    target = configured_wordpress_target()
    if target is None:
        raise SystemExit("Configure a staging WordPress target in development or test.")
    username, password = _credentials()
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        adapter = WordPressStudioClient(client, target, username, password)
        while True:
            if not await run_next_wordpress_operation(async_session_factory, adapter):
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(_main())
