import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    StudioWordPressRevision,
    WordPressAction,
    WordPressState,
)
from app.schemas.content_asset import CreativeMediaObject
from app.services.campaign_studio_service import CampaignStudioService
from app.services.studio_website_package import (
    WebsiteIllustration,
    read_approved_website_article,
    read_website_illustration,
    website_image_bytes,
)
from app.workers.studio_wordpress_execution import (
    LEASE_DURATION,
    OPERATION_TIMEOUT,
    Claim,
    render_article,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RevisionWork:
    phase: str
    previous: WordPressArticle
    replacement: WordPressArticle
    post_id: int
    post_url: str
    previous_image: WordPressImage | None
    previous_media_id: int
    replacement_image: WordPressImage | None
    replacement_media_id: int
    local_media: CreativeMediaObject | None


async def _claim(session: AsyncSession, target: WordPressTarget) -> Claim | None:
    revision = StudioWordPressRevision
    delivery = StudioWordPressDelivery
    now = datetime.now(UTC)
    stale_before = now - LEASE_DURATION
    target_filter = (
        delivery.target_url == target.url,
        delivery.target_author_id == target.author_id,
    )
    await session.execute(
        update(revision)
        .where(
            revision.delivery_id.in_(select(delivery.id).where(*target_filter)),
            revision.status == WordPressState.RUNNING,
            revision.started_at <= stale_before,
        )
        .values(
            status=WordPressState.UNKNOWN,
            finished_at=now,
            failure_code="WORDPRESS_WORKER_LOST",
            lease_id=None,
        )
    )
    candidate = await session.scalar(
        select(revision)
        .join(delivery, delivery.id == revision.delivery_id)
        .where(
            *target_filter,
            or_(
                revision.status == WordPressState.QUEUED,
                and_(
                    revision.status == WordPressState.UNKNOWN,
                    revision.reconcile_requested_at.is_not(None),
                    revision.reconcile_finished_at.is_(None),
                    or_(
                        revision.reconcile_started_at.is_(None),
                        revision.reconcile_started_at <= stale_before,
                    ),
                ),
            ),
        )
        .order_by(revision.requested_at, revision.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=revision)
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


def _image_payload(visual: WebsiteIllustration | None, slug: str | None) -> WordPressImage | None:
    if visual is None and slug is None:
        return None
    if visual is None or slug is None:
        raise WordPressError("WORDPRESS_APPROVED_VERSION_MISMATCH")
    media = visual.media
    return WordPressImage(slug, media.sha256, media.size_bytes, visual.details.alt_text)


async def _load_work(session: AsyncSession, claim: Claim, target: WordPressTarget) -> RevisionWork:
    revision = await session.get(StudioWordPressRevision, claim.operation_id)
    if revision is None or revision.lease_id != claim.lease_id:
        raise WordPressError("WORDPRESS_CLAIM_LOST")
    delivery = await session.get(StudioWordPressDelivery, revision.delivery_id)
    if delivery is None or (delivery.target_url, delivery.target_author_id) != (
        target.url,
        target.author_id,
    ):
        raise WordPressError("WORDPRESS_TARGET_CHANGED")
    user = revision.reconcile_requested_by if claim.reconciliation else revision.requested_by
    if user is None:
        raise WordPressError("WORDPRESS_APPROVAL_NOT_FOUND")
    studio = CampaignStudioService(session)
    await studio.require_campaign_access(user, delivery.campaign_id, write=True)
    original, original_article = await read_approved_website_article(
        studio, user, delivery.campaign_id, delivery.article_version_id
    )
    if original.illustration_version_id != delivery.illustration_version_id:
        raise WordPressError("WORDPRESS_APPROVED_VERSION_MISMATCH")

    receipts = {
        operation.action: operation
        for operation in await session.scalars(
            select(StudioWordPressOperation).where(
                StudioWordPressOperation.delivery_id == delivery.id,
                StudioWordPressOperation.action.in_(
                    (WordPressAction.PUBLISH, WordPressAction.IMAGE)
                ),
                StudioWordPressOperation.status == WordPressState.SUCCEEDED,
            )
        )
    }
    published = receipts.get(WordPressAction.PUBLISH)
    if published is None or published.remote_id is None or published.remote_url is None:
        raise WordPressError("WORDPRESS_PUBLICATION_REQUIRED")

    successor = await session.scalar(
        select(StudioWordPressRevision.id).where(
            StudioWordPressRevision.delivery_id == delivery.id,
            StudioWordPressRevision.expected_revision_id == revision.expected_revision_id,
            StudioWordPressRevision.status == WordPressState.SUCCEEDED,
        )
    )
    if successor is not None:
        raise WordPressError("WORDPRESS_REVISION_STALE")
    slug = f"iteam-{delivery.id.hex}"
    previous_version, previous_article = original, original_article
    previous_media_id = None
    previous_media_slug = None
    if revision.expected_revision_id is None:
        if delivery.illustration_version_id is not None:
            attachment = receipts.get(WordPressAction.IMAGE)
            if attachment is None or attachment.remote_id is None:
                raise WordPressError("WORDPRESS_MEDIA_REQUIRED")
            previous_media_id = attachment.remote_id
            previous_media_slug = f"{slug}-image"
    else:
        previous = await session.get(StudioWordPressRevision, revision.expected_revision_id)
        if (
            previous is None
            or previous.delivery_id != delivery.id
            or previous.status != WordPressState.SUCCEEDED
        ):
            raise WordPressError("WORDPRESS_REVISION_STALE")
        previous_version, previous_article = await read_approved_website_article(
            studio, user, delivery.campaign_id, previous.article_version_id
        )
        previous_media_id = previous.media_id
        previous_media_slug = previous.media_slug
    replacement, replacement_article = await read_approved_website_article(
        studio, user, delivery.campaign_id, revision.article_version_id
    )
    if previous_version.id == replacement.id:
        raise WordPressError("WORDPRESS_REVISION_STALE")
    if previous_version.asset_id != original.asset_id or replacement.asset_id != original.asset_id:
        raise WordPressError("WORDPRESS_APPROVED_VERSION_MISMATCH")

    previous_visual = await read_website_illustration(
        studio, user, delivery.campaign_id, previous_version.illustration_version_id
    )
    replacement_visual = await read_website_illustration(
        studio, user, delivery.campaign_id, replacement.illustration_version_id
    )
    media_slug = (
        f"iteam-{revision.id.hex}-revision-image"
        if revision.phase == "image"
        else revision.media_slug
    )
    previous_image = _image_payload(previous_visual, previous_media_slug)
    replacement_image = _image_payload(replacement_visual, media_slug)
    return RevisionWork(
        revision.phase,
        render_article(slug, previous_article),
        render_article(slug, replacement_article),
        published.remote_id,
        published.remote_url,
        previous_image,
        previous_media_id or 0,
        replacement_image,
        revision.media_id or 0,
        replacement_visual.media if replacement_visual is not None else None,
    )


async def _execute(
    adapter: WordPressStudioClient, work: RevisionWork, *, reconciliation: bool
) -> WordPressPost | None:
    await adapter.verify_actor()
    if work.phase == "image":
        if work.replacement_image is None or work.local_media is None:
            raise WordPressError("WORDPRESS_MEDIA_REQUIRED")
        if reconciliation:
            media_id = await adapter.find_image(work.replacement_image, work.post_id)
            return WordPressPost(media_id) if media_id is not None else None
        if work.previous_image is not None:
            await adapter.verify_image(work.previous_image, work.post_id, work.previous_media_id)
        await adapter.read_published(
            work.previous, work.post_id, work.previous_media_id, work.post_url
        )
        data = await website_image_bytes(work.local_media)
        return WordPressPost(await adapter.upload_image(work.replacement_image, work.post_id, data))

    if work.replacement_image is not None:
        await adapter.verify_image(work.replacement_image, work.post_id, work.replacement_media_id)
    args = (
        work.previous,
        work.replacement,
        work.post_id,
        work.previous_media_id,
        work.replacement_media_id,
        work.post_url,
    )
    if reconciliation:
        return await adapter.find_replacement(*args)
    if work.previous_image is not None and work.previous_media_id != work.replacement_media_id:
        await adapter.verify_image(work.previous_image, work.post_id, work.previous_media_id)
    return await adapter.replace_publication(*args)


def _record_result(
    revision: StudioWordPressRevision,
    claim: Claim,
    receipt: WordPressPost | None,
    failure: str | None,
    *,
    write_attempted: bool,
) -> None:
    revision.lease_id = None
    if receipt is not None and revision.phase == "image":
        revision.media_id = receipt.id
        revision.media_slug = f"iteam-{revision.id.hex}-revision-image"
        revision.phase = "replace"
        revision.status = WordPressState.QUEUED
        revision.started_at = None
        revision.finished_at = None
        revision.failure_code = None
        revision.reconcile_requested_by = None
        revision.reconcile_requested_at = None
        revision.reconcile_started_at = None
        revision.reconcile_finished_at = None
        revision.reconciliation_failure_code = None
        return
    now = datetime.now(UTC)
    if claim.reconciliation:
        revision.reconcile_finished_at = now
        revision.reconciliation_failure_code = failure
    else:
        revision.finished_at = now
        revision.failure_code = failure
    if receipt is not None:
        revision.status = WordPressState.SUCCEEDED
    elif not claim.reconciliation:
        revision.status = WordPressState.UNKNOWN if write_attempted else WordPressState.FAILED


async def run_next_wordpress_revision(
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
        logger.error("Unexpected WordPress revision failure for %s", claim.operation_id)

    async with sessions.begin() as session:
        revision = await session.get(
            StudioWordPressRevision, claim.operation_id, with_for_update=True
        )
        if revision is not None and revision.lease_id == claim.lease_id:
            _record_result(
                revision, claim, receipt, failure, write_attempted=adapter.write_attempted
            )
    return True
