import json
from dataclasses import dataclass
from html import escape
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile, ZipInfo

from app.core.errors import AppError
from app.integrations.creative_media import (
    CreativeMediaNotFoundError,
    CreativeMediaStorageError,
    read_creative_image,
)
from app.schemas.content_asset import (
    ArticleVersionData,
    ContentAssetVersionResponse,
    CreativeMediaObject,
    IllustrationVersionData,
)
from app.services.campaign_studio_service import CampaignStudioService

ARTICLE_FILENAME = "article.html"
IMAGE_FILENAME = "illustration.webp"


@dataclass(frozen=True)
class WebsiteIllustration:
    version: ContentAssetVersionResponse
    details: IllustrationVersionData
    media: CreativeMediaObject


async def read_approved_website_article(
    studio: CampaignStudioService, user_id: UUID, campaign_id: UUID, version_id: UUID
) -> tuple[ContentAssetVersionResponse, ArticleVersionData]:
    article = await studio.read_version(user_id, campaign_id, version_id)
    if not isinstance(article.payload, ArticleVersionData):
        raise AppError("INVALID_ASSET_TYPE", "Выберите версию статьи.", 422)
    if article.approval is None:
        raise AppError("STUDIO_ARTICLE_APPROVAL_REQUIRED", "Сначала утвердите версию статьи.", 409)
    return article, article.payload


async def read_website_illustration(
    studio: CampaignStudioService, user_id: UUID, campaign_id: UUID, version_id: UUID | None
) -> WebsiteIllustration | None:
    if version_id is None:
        return None
    illustration = await studio.read_version(user_id, campaign_id, version_id)
    if not isinstance(illustration.payload, IllustrationVersionData):
        raise AppError("INVALID_ILLUSTRATION", "Укажите версию иллюстрации.", 422)
    if illustration.approval is None:
        raise AppError(
            "ILLUSTRATION_APPROVAL_REQUIRED", "Сначала утвердите версию иллюстрации.", 409
        )
    visual = illustration.payload
    media = next((variant for variant in visual.variants if variant.name == "website"), None)
    if media is None or media.mime_type != "image/webp":
        raise AppError("CREATIVE_MEDIA_NOT_FOUND", "Вариант изображения для сайта не найден.", 404)
    return WebsiteIllustration(
        illustration, visual, CreativeMediaObject.model_validate(media.model_dump(exclude={"name"}))
    )


async def website_image_bytes(media: CreativeMediaObject) -> bytes:
    try:
        return await read_creative_image(media.sha256)
    except CreativeMediaNotFoundError:
        raise AppError("CREATIVE_MEDIA_NOT_FOUND", "Изображение не найдено.", 404) from None
    except CreativeMediaStorageError:
        raise AppError(
            "CREATIVE_MEDIA_UNAVAILABLE", "Хранилище изображений недоступно.", 503
        ) from None


def render_website_figure(
    src: str, alt: str, caption: str | None, media: CreativeMediaObject
) -> str:
    description = f"<figcaption>{escape(caption)}</figcaption>" if caption else ""
    return (
        f'<figure><img src="{escape(src)}" alt="{escape(alt)}" '
        f'width="{media.width}" height="{media.height}" '
        f'style="max-width: 100%; height: auto">{description}</figure>'
    )


def render_website_html(
    article: ArticleVersionData,
    figure: str,
    *,
    seo_title: str | None = None,
    seo_description: str | None = None,
) -> str:
    title = escape(article.title)
    body = escape(article.body)
    page_title = escape(seo_title) if seo_title else title
    metadata = (
        f'<meta name="description" content="{escape(seo_description)}">\n'
        if seo_description
        else ""
    )
    return (
        '<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{metadata}<title>{page_title}</title>\n</head>\n<body>\n<article>\n<h1>{title}</h1>\n"
        f'{figure}\n<div style="white-space: pre-wrap">{body}</div>\n'
        "</article>\n</body>\n</html>\n"
    )


async def build_website_package(
    studio: CampaignStudioService, user_id: UUID, campaign_id: UUID, version_id: UUID
) -> bytes:
    article, content = await read_approved_website_article(studio, user_id, campaign_id, version_id)
    illustration = await read_website_illustration(
        studio, user_id, campaign_id, article.illustration_version_id
    )
    illustration_metadata = None
    image = None
    figure = ""
    if illustration is not None:
        visual, media = illustration.details, illustration.media
        image = await website_image_bytes(media)
        illustration_metadata = {
            **_version_metadata(illustration.version),
            "title": visual.title,
            "file": IMAGE_FILENAME,
            "variant": "website",
            "alt_text": visual.alt_text,
            "caption": visual.caption,
            "usage_rights": visual.usage_rights,
            "media": media.model_dump(),
        }
        figure = render_website_figure(IMAGE_FILENAME, visual.alt_text, visual.caption, media)

    html = render_website_html(content, figure)
    manifest = {
        "format_version": 1,
        "campaign_id": str(campaign_id),
        "article": {
            **_version_metadata(article),
            "title": content.title,
            "file": ARTICLE_FILENAME,
        },
        "illustration": illustration_metadata,
    }
    with BytesIO() as output:
        with ZipFile(output, "w") as archive:
            archive.writestr(ZipInfo(ARTICLE_FILENAME), html.encode("utf-8"))
            archive.writestr(
                ZipInfo("manifest.json"),
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            if image is not None:
                archive.writestr(ZipInfo(IMAGE_FILENAME), image)
        return output.getvalue()


def _version_metadata(version: ContentAssetVersionResponse) -> dict[str, str | int]:
    return {
        "asset_id": str(version.asset_id),
        "version_id": str(version.id),
        "version_number": version.version_number,
        "snapshot_hash": version.snapshot_hash,
    }
