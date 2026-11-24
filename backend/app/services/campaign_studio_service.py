from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.integrations.creative_media import CreativeImageObject, StoredCreativeImage
from app.models.campaign import Campaign, CampaignStatus
from app.models.content_asset import (
    ContentAsset,
    ContentAssetApproval,
    ContentAssetKind,
    ContentAssetVersion,
)
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMembership, WorkspaceRole
from app.schemas.content_asset import (
    ArticleDraft,
    ArticleVersionData,
    ContentAssetApprovalResponse,
    ContentAssetPage,
    ContentAssetResponse,
    ContentAssetVersionPage,
    ContentAssetVersionResponse,
    CreativeMediaObject,
    CreativeMediaVariant,
    IllustrationDetails,
    IllustrationDraft,
    IllustrationProvenance,
    IllustrationVersionData,
)


class CampaignStudioService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def require_campaign_access(
        self, user_id: UUID, campaign_id: UUID, *, write: bool = False
    ) -> None:
        membership = await self.session.scalar(
            select(WorkspaceMembership.id)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.workspace_id == DEFAULT_WORKSPACE_ID,
                WorkspaceMembership.is_active,
                WorkspaceMembership.role.in_(
                    (WorkspaceRole.WORKSPACE_ADMIN, WorkspaceRole.MARKETING_MANAGER)
                ),
                Workspace.is_default,
                Workspace.is_active,
                User.is_active,
            )
        )
        if membership is None:
            raise AppError("STUDIO_ACCESS_DENIED", "Нет доступа к студии кампании.", 403)
        campaign_status = await self.session.scalar(
            select(Campaign.status).where(Campaign.id == campaign_id)
        )
        if campaign_status is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
        if write and campaign_status == CampaignStatus.ARCHIVED:
            raise AppError("CAMPAIGN_ARCHIVED", "Кампания в архиве.", 409)

    async def list_assets(
        self,
        user_id: UUID,
        campaign_id: UUID,
        *,
        limit: int,
        offset: int,
        kind: ContentAssetKind | None = None,
    ) -> ContentAssetPage:
        await self.require_campaign_access(user_id, campaign_id)
        scope = [
            ContentAsset.workspace_id == DEFAULT_WORKSPACE_ID,
            ContentAsset.campaign_id == campaign_id,
        ]
        if kind is not None:
            scope.append(ContentAsset.kind == kind)
        total = await self.session.scalar(
            select(func.count()).select_from(ContentAsset).where(*scope)
        )
        assets = (
            select(ContentAsset.id, ContentAsset.kind, ContentAsset.created_at)
            .where(*scope)
            .order_by(ContentAsset.created_at.desc(), ContentAsset.id.desc())
            .offset(offset)
            .limit(limit)
            .subquery()
        )
        latest_id = (
            select(ContentAssetVersion.id)
            .where(ContentAssetVersion.asset_id == assets.c.id)
            .order_by(ContentAssetVersion.version_number.desc())
            .limit(1)
            .correlate(assets)
            .scalar_subquery()
        )
        rows = await self.session.execute(
            select(ContentAssetVersion, ContentAssetApproval)
            .select_from(assets)
            .join(ContentAssetVersion, ContentAssetVersion.id == latest_id)
            .outerjoin(
                ContentAssetApproval, ContentAssetApproval.version_id == ContentAssetVersion.id
            )
            .order_by(assets.c.created_at.desc(), assets.c.id.desc())
        )
        return ContentAssetPage(
            items=[
                ContentAssetResponse(
                    id=version.asset_id,
                    kind=version.kind,
                    latest_version=self._response(version, approval),
                )
                for version, approval in rows
            ],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def list_versions(
        self, user_id: UUID, campaign_id: UUID, asset_id: UUID, *, limit: int, offset: int
    ) -> ContentAssetVersionPage:
        await self.require_campaign_access(user_id, campaign_id)
        await self._get_asset(campaign_id, asset_id)
        scope = (
            ContentAssetVersion.asset_id == asset_id,
            ContentAssetVersion.workspace_id == DEFAULT_WORKSPACE_ID,
            ContentAssetVersion.campaign_id == campaign_id,
        )
        total = await self.session.scalar(
            select(func.count()).select_from(ContentAssetVersion).where(*scope)
        )
        rows = await self.session.execute(
            select(ContentAssetVersion, ContentAssetApproval)
            .outerjoin(
                ContentAssetApproval, ContentAssetApproval.version_id == ContentAssetVersion.id
            )
            .where(*scope)
            .order_by(ContentAssetVersion.version_number.desc())
            .offset(offset)
            .limit(limit)
        )
        return ContentAssetVersionPage(
            items=[self._response(version, approval) for version, approval in rows],
            total=total or 0,
            limit=limit,
            offset=offset,
        )

    async def read_version(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID
    ) -> ContentAssetVersionResponse:
        await self.require_campaign_access(user_id, campaign_id)
        version = await self._get_version(campaign_id, version_id)
        approval = await self.session.get(ContentAssetApproval, version.id)
        return self._response(version, approval)

    async def create_article(
        self,
        user_id: UUID,
        campaign_id: UUID,
        draft: ArticleDraft,
        *,
        asset_id: UUID | None = None,
        expected_current_version_id: UUID | None = None,
    ) -> ContentAssetVersionResponse:
        await self.require_campaign_access(user_id, campaign_id, write=True)
        await self._check_source(campaign_id, draft.source_version_id)
        if draft.illustration_version_id is not None:
            illustration = await self._get_version(campaign_id, draft.illustration_version_id)
            if illustration.kind != ContentAssetKind.ILLUSTRATION:
                raise AppError("INVALID_ILLUSTRATION", "Укажите версию иллюстрации.", 422)
        return await self._add_version(
            user_id,
            campaign_id,
            ContentAssetKind.ARTICLE,
            ArticleVersionData(title=draft.title, body=draft.body),
            asset_id=asset_id,
            expected_current_version_id=expected_current_version_id,
            source_version_id=draft.source_version_id,
            illustration_version_id=draft.illustration_version_id,
        )

    async def validate_illustration_target(
        self,
        user_id: UUID,
        campaign_id: UUID,
        draft: IllustrationDraft,
        *,
        asset_id: UUID | None = None,
        expected_current_version_id: UUID | None = None,
    ) -> None:
        await self.require_campaign_access(user_id, campaign_id, write=True)
        await self._check_source(campaign_id, draft.source_version_id)
        await self._revision_head(
            campaign_id,
            asset_id,
            expected_current_version_id,
            ContentAssetKind.ILLUSTRATION,
            lock=False,
        )

    async def create_illustration(
        self,
        user_id: UUID,
        campaign_id: UUID,
        draft: IllustrationDraft,
        stored: StoredCreativeImage,
        provenance: IllustrationProvenance,
        *,
        asset_id: UUID | None = None,
        expected_current_version_id: UUID | None = None,
    ) -> ContentAssetVersionResponse:
        await self.require_campaign_access(user_id, campaign_id, write=True)
        await self._check_source(campaign_id, draft.source_version_id)
        details = IllustrationDetails(**draft.model_dump(exclude={"source_version_id"}))
        payload = IllustrationVersionData(
            **details.model_dump(),
            original=self._media(stored.original),
            variants=[
                CreativeMediaVariant(name=variant.name, **self._media(variant.image).model_dump())
                for variant in stored.variants
            ],
            provenance=provenance,
        )
        return await self._add_version(
            user_id,
            campaign_id,
            ContentAssetKind.ILLUSTRATION,
            payload,
            asset_id=asset_id,
            expected_current_version_id=expected_current_version_id,
            source_version_id=draft.source_version_id,
        )

    async def revise_illustration_details(
        self,
        user_id: UUID,
        campaign_id: UUID,
        draft: IllustrationDraft,
        *,
        asset_id: UUID,
        expected_current_version_id: UUID,
    ) -> ContentAssetVersionResponse:
        await self.require_campaign_access(user_id, campaign_id, write=True)
        await self._check_source(campaign_id, draft.source_version_id)
        previous = await self._get_version(campaign_id, expected_current_version_id)
        if previous.kind != ContentAssetKind.ILLUSTRATION:
            raise AppError("INVALID_ILLUSTRATION", "Укажите версию иллюстрации.", 422)
        saved = IllustrationVersionData.model_validate(previous.payload)
        payload = IllustrationVersionData(
            **draft.model_dump(exclude={"source_version_id"}),
            original=saved.original,
            variants=saved.variants,
            provenance=saved.provenance,
        )
        return await self._add_version(
            user_id,
            campaign_id,
            ContentAssetKind.ILLUSTRATION,
            payload,
            asset_id=asset_id,
            expected_current_version_id=expected_current_version_id,
            source_version_id=draft.source_version_id,
        )

    async def approve(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID
    ) -> ContentAssetVersionResponse:
        await self.require_campaign_access(user_id, campaign_id, write=True)
        version = await self._get_version(campaign_id, version_id, lock=True)
        approval = await self.session.get(ContentAssetApproval, version.id)
        if approval is None:
            if (
                version.illustration_version_id is not None
                and await self.session.get(ContentAssetApproval, version.illustration_version_id)
                is None
            ):
                raise AppError(
                    "ILLUSTRATION_APPROVAL_REQUIRED",
                    "Сначала согласуйте указанную версию иллюстрации.",
                    409,
                )
            approval = ContentAssetApproval(
                version_id=version.id,
                workspace_id=version.workspace_id,
                campaign_id=version.campaign_id,
                snapshot_hash=version.snapshot_hash,
                approved_by=user_id,
            )
            self.session.add(approval)
            await self.session.flush()
        return self._response(version, approval)

    async def media_object(
        self, user_id: UUID, campaign_id: UUID, version_id: UUID, name: str
    ) -> CreativeMediaObject:
        await self.require_campaign_access(user_id, campaign_id)
        version = await self._get_version(campaign_id, version_id)
        if version.kind == ContentAssetKind.ILLUSTRATION:
            payload = IllustrationVersionData.model_validate(version.payload)
            if name == "original":
                return payload.original
            for variant in payload.variants:
                if variant.name == name:
                    return variant
        raise AppError("CREATIVE_MEDIA_NOT_FOUND", "Изображение не найдено.", 404)

    async def _add_version(
        self,
        user_id: UUID,
        campaign_id: UUID,
        kind: ContentAssetKind,
        payload: ArticleVersionData | IllustrationVersionData,
        *,
        asset_id: UUID | None,
        expected_current_version_id: UUID | None,
        source_version_id: UUID | None,
        illustration_version_id: UUID | None = None,
    ) -> ContentAssetVersionResponse:
        head = await self._revision_head(
            campaign_id, asset_id, expected_current_version_id, kind, lock=True
        )
        if asset_id is None:
            asset = ContentAsset(
                workspace_id=DEFAULT_WORKSPACE_ID,
                campaign_id=campaign_id,
                kind=kind,
                created_by=user_id,
            )
            self.session.add(asset)
            await self.session.flush()
            asset_id = asset.id
        version = ContentAssetVersion(
            asset_id=asset_id,
            workspace_id=DEFAULT_WORKSPACE_ID,
            campaign_id=campaign_id,
            kind=kind,
            version_number=head.version_number + 1 if head else 1,
            supersedes_version_id=head.id if head else None,
            source_version_id=source_version_id,
            illustration_version_id=illustration_version_id,
            created_by=user_id,
            payload=payload.model_dump(mode="json"),
        )
        self.session.add(version)
        await self.session.flush()
        return self._response(version, None)

    async def _revision_head(
        self,
        campaign_id: UUID,
        asset_id: UUID | None,
        expected_current_version_id: UUID | None,
        kind: ContentAssetKind,
        *,
        lock: bool,
    ) -> ContentAssetVersion | None:
        if (asset_id is None) != (expected_current_version_id is None):
            raise AppError("INVALID_STUDIO_REVISION", "Укажите материал и исходную версию.", 422)
        if asset_id is None:
            return None
        asset = await self._get_asset(campaign_id, asset_id, lock=lock)
        if asset.kind != kind:
            raise AppError("INVALID_ASSET_TYPE", "Тип материала не совпадает.", 422)
        head = await self.session.scalar(
            select(ContentAssetVersion)
            .where(ContentAssetVersion.asset_id == asset_id)
            .order_by(ContentAssetVersion.version_number.desc())
            .limit(1)
        )
        if head is None or head.id != expected_current_version_id:
            raise AppError(
                "STUDIO_VERSION_CONFLICT", "Материал изменился. Откройте новую версию.", 409
            )
        return head

    async def _get_asset(
        self, campaign_id: UUID, asset_id: UUID, *, lock: bool = False
    ) -> ContentAsset:
        query = select(ContentAsset).where(
            ContentAsset.id == asset_id,
            ContentAsset.campaign_id == campaign_id,
            ContentAsset.workspace_id == DEFAULT_WORKSPACE_ID,
        )
        asset = await self.session.scalar(query.with_for_update() if lock else query)
        if asset is None:
            raise AppError("STUDIO_ASSET_NOT_FOUND", "Материал не найден.", 404)
        return asset

    async def _get_version(
        self, campaign_id: UUID, version_id: UUID, *, lock: bool = False
    ) -> ContentAssetVersion:
        query = select(ContentAssetVersion).where(
            ContentAssetVersion.id == version_id,
            ContentAssetVersion.campaign_id == campaign_id,
            ContentAssetVersion.workspace_id == DEFAULT_WORKSPACE_ID,
        )
        version = await self.session.scalar(query.with_for_update() if lock else query)
        if version is None:
            raise AppError("STUDIO_VERSION_NOT_FOUND", "Версия материала не найдена.", 404)
        return version

    async def _check_source(self, campaign_id: UUID, source_version_id: UUID | None) -> None:
        if source_version_id is not None:
            await self._get_version(campaign_id, source_version_id)

    @staticmethod
    def _response(
        version: ContentAssetVersion, approval: ContentAssetApproval | None
    ) -> ContentAssetVersionResponse:
        return ContentAssetVersionResponse(
            id=version.id,
            asset_id=version.asset_id,
            kind=version.kind,
            version_number=version.version_number,
            supersedes_version_id=version.supersedes_version_id,
            source_version_id=version.source_version_id,
            illustration_version_id=version.illustration_version_id,
            snapshot_hash=version.snapshot_hash,
            created_by=version.created_by,
            created_at=version.created_at,
            approval=ContentAssetApprovalResponse.model_validate(approval) if approval else None,
            payload=(
                ArticleVersionData.model_validate(version.payload)
                if version.kind == ContentAssetKind.ARTICLE
                else IllustrationVersionData.model_validate(version.payload)
            ),
        )

    @staticmethod
    def _media(image: CreativeImageObject) -> CreativeMediaObject:
        return CreativeMediaObject(
            sha256=image.digest,
            mime_type=image.mime_type,
            size_bytes=image.byte_size,
            width=image.width,
            height=image.height,
        )
