import json
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
    IllustrationVersionData,
)
from app.services.campaign_studio_service import CampaignStudioService

ARTICLE_FILENAME = "article.html"
IMAGE_FILENAME = "illustration.webp"


async def build_website_package(
    studio: CampaignStudioService, user_id: UUID, campaign_id: UUID, version_id: UUID
) -> bytes:
    article = await studio.read_version(user_id, campaign_id, version_id)
    if not isinstance(article.payload, ArticleVersionData):
        raise AppError("INVALID_ASSET_TYPE", "Выберите версию статьи.", 422)
    if article.approval is None:
        raise AppError("STUDIO_ARTICLE_APPROVAL_REQUIRED", "Сначала утвердите версию статьи.", 409)

    illustration_metadata = None
    image = None
    figure = ""
    if article.illustration_version_id is not None:
        illustration = await studio.read_version(
            user_id, campaign_id, article.illustration_version_id
        )
        if not isinstance(illustration.payload, IllustrationVersionData):
            raise AppError("INVALID_ILLUSTRATION", "Укажите версию иллюстрации.", 422)
        if illustration.approval is None:
            raise AppError(
                "ILLUSTRATION_APPROVAL_REQUIRED", "Сначала утвердите версию иллюстрации.", 409
            )
        visual = illustration.payload
        media = next((variant for variant in visual.variants if variant.name == "website"), None)
        if media is None or media.mime_type != "image/webp":
            raise AppError(
                "CREATIVE_MEDIA_NOT_FOUND", "Вариант изображения для сайта не найден.", 404
            )
        try:
            image = await read_creative_image(media.sha256)
        except CreativeMediaNotFoundError:
            raise AppError("CREATIVE_MEDIA_NOT_FOUND", "Изображение не найдено.", 404) from None
        except CreativeMediaStorageError:
            raise AppError(
                "CREATIVE_MEDIA_UNAVAILABLE", "Хранилище изображений недоступно.", 503
            ) from None
        illustration_metadata = {
            **_version_metadata(illustration),
            "title": visual.title,
            "file": IMAGE_FILENAME,
            "variant": "website",
            "alt_text": visual.alt_text,
            "caption": visual.caption,
            "usage_rights": visual.usage_rights,
            "media": media.model_dump(exclude={"name"}),
        }
        caption = f"<figcaption>{escape(visual.caption)}</figcaption>" if visual.caption else ""
        figure = (
            f'<figure><img src="{IMAGE_FILENAME}" alt="{escape(visual.alt_text)}" '
            f'width="{media.width}" height="{media.height}" '
            f'style="max-width: 100%; height: auto">{caption}</figure>'
        )

    title = escape(article.payload.title)
    body = escape(article.payload.body)
    html = (
        '<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n</head>\n<body>\n<article>\n<h1>{title}</h1>\n"
        f'{figure}\n<div style="white-space: pre-wrap">{body}</div>\n'
        "</article>\n</body>\n</html>\n"
    )
    manifest = {
        "format_version": 1,
        "campaign_id": str(campaign_id),
        "article": {
            **_version_metadata(article),
            "title": article.payload.title,
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
