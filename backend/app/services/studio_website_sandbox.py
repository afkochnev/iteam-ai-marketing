from base64 import b64encode
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.models.studio_website import StudioWebsitePage
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.schemas.content_asset import ArticleVersionData, CreativeMediaObject, StudioTextFields
from app.services.campaign_studio_service import CampaignStudioService
from app.services.studio_website_package import (
    read_approved_website_article,
    read_website_illustration,
    render_website_figure,
    render_website_html,
    website_image_bytes,
)


class WebsiteDraft(StudioTextFields):
    seo_title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)] = ""
    seo_description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=320)] = ""


class WebsitePage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    article_version_id: UUID
    illustration_version_id: UUID | None
    created_at: datetime
    published_at: datetime | None


class WebsiteStatus(BaseModel):
    enabled: bool
    page: WebsitePage | None = None


class WebsiteImage(BaseModel):
    snapshot_hash: str
    media: CreativeMediaObject
    alt: str
    caption: str | None
    usage_rights: str


class WebsiteSnapshot(BaseModel):
    article: ArticleVersionData
    article_snapshot_hash: str
    illustration: WebsiteImage | None
    metadata: WebsiteDraft


def website_sandbox_enabled() -> bool:
    return settings.studio_website_sandbox_enabled and settings.app_env.lower() in {
        "development",
        "test",
    }


class WebsiteSandbox:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.studio = CampaignStudioService(session)

    async def status(self, user_id: UUID, campaign_id: UUID, version_id: UUID) -> WebsiteStatus:
        if not website_sandbox_enabled():
            return WebsiteStatus(enabled=False)
        await read_approved_website_article(self.studio, user_id, campaign_id, version_id)
        page = await self._find_page(campaign_id, version_id)
        return WebsiteStatus(enabled=True, page=WebsitePage.model_validate(page) if page else None)

    async def create_draft(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID, metadata: WebsiteDraft
    ) -> WebsitePage:
        self._require_enabled()
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        approved, article = await read_approved_website_article(
            self.studio, user_id, campaign_id, version_id
        )
        page = await self._find_page(campaign_id, version_id)
        if page is None:
            illustration = await read_website_illustration(
                self.studio, user_id, campaign_id, approved.illustration_version_id
            )
            image = None
            if illustration is not None:
                await website_image_bytes(illustration.media)
                image = WebsiteImage(
                    snapshot_hash=illustration.version.snapshot_hash,
                    media=illustration.media,
                    alt=illustration.details.alt_text,
                    caption=illustration.details.caption,
                    usage_rights=illustration.details.usage_rights,
                )
            snapshot = WebsiteSnapshot(
                article=article,
                article_snapshot_hash=approved.snapshot_hash,
                illustration=image,
                metadata=metadata,
            )
            statement = (
                insert(StudioWebsitePage)
                .values(
                    article_version_id=version_id,
                    illustration_version_id=approved.illustration_version_id,
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    campaign_id=campaign_id,
                    snapshot=snapshot.model_dump(mode="json"),
                    created_by=user_id,
                )
                .on_conflict_do_nothing(index_elements=[StudioWebsitePage.article_version_id])
                .returning(StudioWebsitePage)
            )
            page = await self.session.scalar(statement) or await self._find_page(
                campaign_id, version_id
            )
        if page is None:
            raise RuntimeError("Website draft insert did not return a page")
        if WebsiteSnapshot.model_validate(page.snapshot).metadata != metadata:
            raise AppError(
                "WEBSITE_DRAFT_ALREADY_EXISTS",
                "Для версии уже есть черновик с другими SEO-полями.",
                409,
            )
        return WebsitePage.model_validate(page)

    async def publish(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID, page_id: UUID
    ) -> WebsitePage:
        self._require_enabled()
        await self.studio.require_campaign_access(user_id, campaign_id, write=True)
        page = await self._find_page(campaign_id, version_id, lock=True)
        if page is None or page.id != page_id:
            raise self._not_found()
        if page.published_at is not None:
            return WebsitePage.model_validate(page)

        snapshot = WebsiteSnapshot.model_validate(page.snapshot)
        if snapshot.illustration is not None:
            await website_image_bytes(snapshot.illustration.media)
        page.published_by = user_id
        page.published_at = await self.session.scalar(select(func.now()))
        await self.session.flush()
        return WebsitePage.model_validate(page)

    async def preview(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID, page_id: UUID
    ) -> str:
        self._require_enabled()
        await self.studio.require_campaign_access(user_id, campaign_id)
        page = await self._find_page(campaign_id, version_id)
        if page is None or page.id != page_id:
            raise self._not_found()
        return await self._html(page)

    async def public_page(self, page_id: UUID) -> str:
        self._require_enabled()
        page = await self.session.scalar(
            select(StudioWebsitePage).where(
                StudioWebsitePage.id == page_id, StudioWebsitePage.published_at.is_not(None)
            )
        )
        if page is None:
            raise self._not_found()
        return await self._html(page)

    @staticmethod
    async def _html(page: StudioWebsitePage) -> str:
        snapshot = WebsiteSnapshot.model_validate(page.snapshot)
        figure = ""
        if snapshot.illustration is not None:
            image = snapshot.illustration
            encoded = b64encode(await website_image_bytes(image.media)).decode("ascii")
            figure = render_website_figure(
                f"data:image/webp;base64,{encoded}", image.alt, image.caption, image.media
            )
        return render_website_html(
            snapshot.article,
            figure,
            seo_title=snapshot.metadata.seo_title,
            seo_description=snapshot.metadata.seo_description,
        )

    async def _find_page(
        self, campaign_id: UUID, version_id: UUID, *, lock: bool = False
    ) -> StudioWebsitePage | None:
        query = select(StudioWebsitePage).where(
            StudioWebsitePage.campaign_id == campaign_id,
            StudioWebsitePage.workspace_id == DEFAULT_WORKSPACE_ID,
            StudioWebsitePage.article_version_id == version_id,
        )
        return await self.session.scalar(query.with_for_update() if lock else query)

    @staticmethod
    def _require_enabled() -> None:
        if not website_sandbox_enabled():
            raise AppError("WEBSITE_SANDBOX_DISABLED", "Тестовая CMS отключена.", 404)

    @staticmethod
    def _not_found() -> AppError:
        return AppError("WEBSITE_PAGE_NOT_FOUND", "Страница тестовой CMS не найдена.", 404)
