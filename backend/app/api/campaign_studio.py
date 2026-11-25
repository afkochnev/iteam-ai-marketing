from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from kombu.exceptions import KombuError
from pydantic import ValidationError

from app.api.dependencies import (
    CurrentSnapshotUser,
    CurrentUser,
    ReadSnapshotSession,
    SessionDependency,
)
from app.core.errors import AppError
from app.integrations.creative_media import (
    MAX_CREATIVE_IMAGE_BYTES,
    CreativeImageValidationError,
    CreativeMediaNotFoundError,
    CreativeMediaStorageError,
    read_creative_image,
    store_creative_image,
)
from app.models.content_asset import ContentAssetKind
from app.schemas.content_asset import (
    AltText,
    ArticleDraft,
    ArticleRevision,
    AssetTitle,
    BrandProfile,
    ContentAssetPage,
    ContentAssetVersionPage,
    ContentAssetVersionResponse,
    CreativeBrief,
    CreativeNotes,
    IllustrationDraft,
    IllustrationGenerationRequest,
    IllustrationProvenance,
    UsageRights,
)
from app.services.campaign_studio_service import CampaignStudioService
from app.services.creative_generation_service import (
    CreativeGenerationService,
    GenerationJob,
    GenerationJobDetails,
)

router = APIRouter(prefix="/campaigns/{campaign_id}/studio", tags=["campaign-studio"])
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=2_147_483_647)]


@dataclass(frozen=True)
class IllustrationUpload:
    draft: IllustrationDraft
    file: UploadFile | None
    expected_current_version_id: UUID | None


async def illustration_upload(
    title: Annotated[AssetTitle, Form()],
    brief: Annotated[CreativeBrief, Form()],
    brand_profile: Annotated[BrandProfile, Form()],
    alt_text: Annotated[AltText, Form()],
    usage_rights: Annotated[UsageRights, Form()],
    file: Annotated[UploadFile | None, File()] = None,
    caption: Annotated[CreativeNotes | None, Form()] = None,
    source_version_id: Annotated[UUID | None, Form()] = None,
    expected_current_version_id: Annotated[UUID | None, Form()] = None,
) -> IllustrationUpload:
    try:
        draft = IllustrationDraft(
            title=title,
            brief=brief,
            brand_profile=brand_profile,
            alt_text=alt_text,
            usage_rights=usage_rights,
            caption=caption,
            source_version_id=source_version_id,
        )
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from None
    return IllustrationUpload(draft, file, expected_current_version_id)


UploadDependency = Annotated[IllustrationUpload, Depends(illustration_upload)]


@router.get("/assets", response_model=ContentAssetPage)
async def list_studio_assets(
    campaign_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    kind: ContentAssetKind | None = None,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> ContentAssetPage:
    return await CampaignStudioService(session).list_assets(
        user.id, campaign_id, kind=kind, limit=limit, offset=offset
    )


@router.get("/assets/{asset_id}/versions", response_model=ContentAssetVersionPage)
async def list_studio_versions(
    campaign_id: UUID,
    asset_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    limit: PageLimit = 50,
    offset: PageOffset = 0,
) -> ContentAssetVersionPage:
    return await CampaignStudioService(session).list_versions(
        user.id, campaign_id, asset_id, limit=limit, offset=offset
    )


@router.get("/versions/{version_id}", response_model=ContentAssetVersionResponse)
async def get_studio_version(
    campaign_id: UUID,
    version_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    response: Response,
) -> ContentAssetVersionResponse:
    version = await CampaignStudioService(session).read_version(user.id, campaign_id, version_id)
    response.headers["Cache-Control"] = "private, no-store"
    return version


@router.post("/assets/articles", status_code=201, response_model=ContentAssetVersionResponse)
async def create_studio_article(
    campaign_id: UUID, draft: ArticleDraft, user: CurrentUser, session: SessionDependency
) -> ContentAssetVersionResponse:
    version = await CampaignStudioService(session).create_article(user.id, campaign_id, draft)
    await session.commit()
    return version


@router.post(
    "/assets/articles/{asset_id}/versions",
    status_code=201,
    response_model=ContentAssetVersionResponse,
)
async def revise_studio_article(
    campaign_id: UUID,
    asset_id: UUID,
    draft: ArticleRevision,
    user: CurrentUser,
    session: SessionDependency,
) -> ContentAssetVersionResponse:
    version = await CampaignStudioService(session).create_article(
        user.id,
        campaign_id,
        draft,
        asset_id=asset_id,
        expected_current_version_id=draft.expected_current_version_id,
    )
    await session.commit()
    return version


@router.post("/assets/illustrations", status_code=201, response_model=ContentAssetVersionResponse)
async def create_studio_illustration(
    campaign_id: UUID, upload: UploadDependency, user: CurrentUser, session: SessionDependency
) -> ContentAssetVersionResponse:
    return await _save_upload(campaign_id, None, upload, user.id, session)


@router.post(
    "/assets/illustrations/{asset_id}/versions",
    status_code=201,
    response_model=ContentAssetVersionResponse,
)
async def revise_studio_illustration(
    campaign_id: UUID,
    asset_id: UUID,
    upload: UploadDependency,
    user: CurrentUser,
    session: SessionDependency,
) -> ContentAssetVersionResponse:
    return await _save_upload(campaign_id, asset_id, upload, user.id, session)


async def _save_upload(
    campaign_id: UUID,
    asset_id: UUID | None,
    upload: IllustrationUpload,
    user_id: UUID,
    session: SessionDependency,
) -> ContentAssetVersionResponse:
    service = CampaignStudioService(session)
    expected_id = upload.expected_current_version_id
    await service.validate_illustration_target(
        user_id,
        campaign_id,
        upload.draft,
        asset_id=asset_id,
        expected_current_version_id=expected_id,
    )
    if upload.file is None:
        if asset_id is None or expected_id is None:
            raise AppError("CREATIVE_IMAGE_REQUIRED", "Загрузите файл иллюстрации.", 422)
        revised = await service.revise_illustration_details(
            user_id,
            campaign_id,
            upload.draft,
            asset_id=asset_id,
            expected_current_version_id=expected_id,
        )
        await session.commit()
        return revised
    try:
        data = await upload.file.read(MAX_CREATIVE_IMAGE_BYTES + 1)
        stored = await store_creative_image(data)
    except CreativeImageValidationError as error:
        raise AppError(
            error.code, "Проверьте формат, размер и содержимое изображения.", 422
        ) from None
    except (CreativeMediaStorageError, OSError):
        raise AppError(
            "CREATIVE_MEDIA_UNAVAILABLE", "Хранилище изображений недоступно.", 503
        ) from None
    version = await service.create_illustration(
        user_id,
        campaign_id,
        upload.draft,
        stored,
        IllustrationProvenance(source="UPLOAD"),
        asset_id=asset_id,
        expected_current_version_id=expected_id,
    )
    await session.commit()
    return version


@router.post("/versions/{version_id}/approve", response_model=ContentAssetVersionResponse)
async def approve_studio_version(
    campaign_id: UUID, version_id: UUID, user: CurrentUser, session: SessionDependency
) -> ContentAssetVersionResponse:
    version = await CampaignStudioService(session).approve(user.id, campaign_id, version_id)
    await session.commit()
    return version


@router.get("/versions/{version_id}/media/{name}", response_class=Response)
async def preview_studio_media(
    campaign_id: UUID,
    version_id: UUID,
    name: str,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
) -> Response:
    media = await CampaignStudioService(session).media_object(
        user.id, campaign_id, version_id, name
    )
    try:
        image = await read_creative_image(media.sha256)
    except CreativeMediaNotFoundError:
        raise AppError("CREATIVE_MEDIA_NOT_FOUND", "Изображение не найдено.", 404) from None
    except CreativeMediaStorageError:
        raise AppError(
            "CREATIVE_MEDIA_UNAVAILABLE", "Хранилище изображений недоступно.", 503
        ) from None
    return Response(
        content=image,
        media_type=media.mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/illustrations/generate", status_code=202, response_model=GenerationJob)
async def request_studio_generation(
    campaign_id: UUID,
    request: IllustrationGenerationRequest,
    user: CurrentUser,
    session: SessionDependency,
) -> GenerationJob:
    from app.workers.creative_generation_tasks import dispatch_creative_generation

    draft = IllustrationDraft.model_validate(
        request.model_dump(exclude={"asset_id", "expected_current_version_id"})
    )
    service = CreativeGenerationService(session)
    user_id = user.id
    job = await service.create_job(
        user_id,
        campaign_id,
        draft,
        prompt=f"Бриф иллюстрации\n{draft.brief}\n\nПравила бренда\n{draft.brand_profile}",
        size="1536x1024",
        asset_id=request.asset_id,
        expected_current_version_id=request.expected_current_version_id,
    )
    await session.commit()
    try:
        dispatch_creative_generation(job.id)
    except (KombuError, OSError):
        failed = await service.mark_dispatch_failed(job.id)
        await session.commit()
        return failed or await service.get_job(user_id, campaign_id, job.id)
    return job


@router.get("/illustrations/generation-jobs/{job_id}", response_model=GenerationJobDetails)
async def get_studio_generation(
    campaign_id: UUID,
    job_id: UUID,
    user: CurrentSnapshotUser,
    session: ReadSnapshotSession,
    response: Response,
) -> GenerationJobDetails:
    result = await CreativeGenerationService(session).get_job_details(user.id, campaign_id, job_id)
    response.headers["Cache-Control"] = "private, no-store"
    return result
