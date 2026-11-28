from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.integrations.studio_wordpress import WordPressTarget
from app.models.content_asset import ContentAssetApproval, ContentAssetKind, ContentAssetVersion
from app.models.studio_wordpress import (
    StudioWordPressDelivery,
    StudioWordPressOperation,
    StudioWordPressRevision,
    WordPressAction,
    WordPressState,
)
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.schemas.publication import CampaignWordPressPerformanceResponse, WordPressPagePlacement
from app.services.campaign_studio_service import CampaignStudioService
from app.services.studio_wordpress import wordpress_staging_enabled


class WordPressPagePlacementService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def read(self, user_id: UUID, campaign_id: UUID) -> CampaignWordPressPerformanceResponse:
        await CampaignStudioService(self.session).require_campaign_access(user_id, campaign_id)
        if not wordpress_staging_enabled():
            return CampaignWordPressPerformanceResponse()

        delivery = StudioWordPressDelivery
        publish = StudioWordPressOperation
        revision = StudioWordPressRevision
        child = aliased(StudioWordPressRevision)
        original = aliased(ContentAssetVersion)
        current = aliased(ContentAssetVersion)
        has_successful_child = (
            select(child.id)
            .where(
                child.delivery_id == revision.delivery_id,
                child.expected_revision_id == revision.id,
                child.status == WordPressState.SUCCEEDED,
            )
            .correlate(revision)
            .exists()
        )

        rows = await self.session.execute(
            select(
                delivery.id.label("placement_id"),
                delivery.target_url.label("site_url"),
                delivery.target_author_id.label("author_id"),
                publish.remote_id.label("post_id"),
                publish.remote_url.label("canonical_url"),
                original.asset_id.label("article_asset_id"),
                original.id.label("original_version_id"),
                current.id.label("current_version_id"),
                current.version_number.label("current_version_number"),
                current.payload["title"].astext.label("current_title"),
                revision.id.label("current_revision_id"),
                publish.finished_at.label("published_at"),
            )
            .select_from(delivery)
            .join(publish, publish.delivery_id == delivery.id)
            .join(original, original.id == delivery.article_version_id)
            .outerjoin(
                revision,
                and_(
                    revision.delivery_id == delivery.id,
                    revision.status == WordPressState.SUCCEEDED,
                    ~has_successful_child,
                ),
            )
            .join(
                current,
                and_(
                    current.id == func.coalesce(revision.article_version_id, original.id),
                    current.asset_id == original.asset_id,
                    current.workspace_id == delivery.workspace_id,
                    current.campaign_id == delivery.campaign_id,
                    current.kind == ContentAssetKind.ARTICLE,
                ),
            )
            .join(ContentAssetApproval, ContentAssetApproval.version_id == current.id)
            .where(
                delivery.workspace_id == DEFAULT_WORKSPACE_ID,
                delivery.campaign_id == campaign_id,
                original.kind == ContentAssetKind.ARTICLE,
                publish.action == WordPressAction.PUBLISH,
                publish.status == WordPressState.SUCCEEDED,
                publish.remote_id > 0,
                publish.remote_url.is_not(None),
                publish.finished_at.is_not(None),
            )
            .order_by(publish.finished_at.desc(), delivery.id)
        )
        placements = []
        for row in rows:
            target = WordPressTarget.from_config(row.site_url, row.author_id)
            if target is None or not target.contains_url(row.canonical_url):
                continue
            placements.append(
                WordPressPagePlacement(
                    placement_id=row.placement_id,
                    site_url=target.url,
                    post_id=str(row.post_id),
                    canonical_url=row.canonical_url,
                    article_asset_id=row.article_asset_id,
                    original_version_id=row.original_version_id,
                    current_version_id=row.current_version_id,
                    current_version_number=row.current_version_number,
                    current_title=row.current_title,
                    current_revision_id=row.current_revision_id,
                    published_at=row.published_at,
                )
            )
        return CampaignWordPressPerformanceResponse(placements=placements)
