from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.creative_media import CreativeImageSize, StoredCreativeImage
from app.models.creative_generation import CreativeGenerationJob, CreativeGenerationStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.schemas.content_asset import IllustrationDraft, IllustrationProvenance
from app.services.campaign_studio_service import CampaignStudioService

PENDING_LIFETIME = timedelta(minutes=30)
RUNNING_LIFETIME = timedelta(minutes=5)


class GenerationJob(BaseModel):
    id: UUID
    status: CreativeGenerationStatus
    asset_id: UUID | None
    version_id: UUID | None
    error_code: str | None


class CreativeGenerationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_job(
        self,
        user_id: UUID,
        campaign_id: UUID,
        draft: IllustrationDraft,
        *,
        prompt: str,
        size: CreativeImageSize,
        asset_id: UUID | None = None,
        expected_current_version_id: UUID | None = None,
    ) -> GenerationJob:
        await CampaignStudioService(self.session).validate_illustration_target(
            user_id,
            campaign_id,
            draft,
            asset_id=asset_id,
            expected_current_version_id=expected_current_version_id,
        )
        model = settings.creative_image_model
        if not model:
            raise AppError(
                "CREATIVE_GENERATION_UNAVAILABLE", "Генерация изображений недоступна.", 503
            )
        if not prompt.strip() or len(prompt) > 16000:
            raise AppError(
                "CREATIVE_GENERATION_INVALID_PROMPT", "Некорректное описание изображения.", 422
            )
        job = CreativeGenerationJob(
            campaign_id=campaign_id,
            workspace_id=DEFAULT_WORKSPACE_ID,
            requested_by=user_id,
            draft=draft.model_dump(mode="json", exclude={"source_version_id"}),
            source_version_id=draft.source_version_id,
            asset_id=asset_id,
            expected_current_version_id=expected_current_version_id,
            prompt=prompt,
            model=model,
            size=size,
            status=CreativeGenerationStatus.PENDING,
        )
        self.session.add(job)
        await self.session.flush()
        return self._dto(job)

    async def get_job(self, user_id: UUID, campaign_id: UUID, job_id: UUID) -> GenerationJob:
        await CampaignStudioService(self.session).require_campaign_access(user_id, campaign_id)
        job = await self.session.scalar(
            select(CreativeGenerationJob).where(
                CreativeGenerationJob.id == job_id,
                CreativeGenerationJob.workspace_id == DEFAULT_WORKSPACE_ID,
                CreativeGenerationJob.campaign_id == campaign_id,
            )
        )
        if job is None:
            raise AppError("CREATIVE_GENERATION_NOT_FOUND", "Задача генерации не найдена.", 404)
        return self._dto(job)

    async def mark_dispatch_failed(self, job_id: UUID) -> GenerationJob | None:
        job = await self.session.scalar(
            update(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.id == job_id,
                CreativeGenerationJob.workspace_id == DEFAULT_WORKSPACE_ID,
                CreativeGenerationJob.status == CreativeGenerationStatus.PENDING,
            )
            .values(
                status=CreativeGenerationStatus.FAILED,
                error_code="CREATIVE_GENERATION_QUEUE_UNAVAILABLE",
                finished_at=func.now(),
            )
            .returning(CreativeGenerationJob)
        )
        return self._dto(job) if job is not None else None

    async def claim(self, job_id: UUID) -> CreativeGenerationJob | None:
        return await self.session.scalar(
            update(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.id == job_id,
                CreativeGenerationJob.workspace_id == DEFAULT_WORKSPACE_ID,
                CreativeGenerationJob.status == CreativeGenerationStatus.PENDING,
                CreativeGenerationJob.created_at >= datetime.now(UTC) - PENDING_LIFETIME,
            )
            .values(
                status=CreativeGenerationStatus.RUNNING,
                claim_token=uuid4(),
                started_at=func.now(),
            )
            .returning(CreativeGenerationJob)
        )

    async def validate_claim(self, job: CreativeGenerationJob) -> None:
        await CampaignStudioService(self.session).validate_illustration_target(
            job.requested_by,
            job.campaign_id,
            self._draft(job),
            asset_id=job.asset_id,
            expected_current_version_id=job.expected_current_version_id,
        )

    async def finish(
        self,
        job_id: UUID,
        claim_token: UUID,
        stored: StoredCreativeImage,
        provenance: IllustrationProvenance,
    ) -> None:
        owned = await self.session.scalar(
            select(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.id == job_id,
                CreativeGenerationJob.workspace_id == DEFAULT_WORKSPACE_ID,
                CreativeGenerationJob.status == CreativeGenerationStatus.RUNNING,
                CreativeGenerationJob.claim_token == claim_token,
            )
            .with_for_update()
        )
        if owned is None:
            return
        version = await CampaignStudioService(self.session).create_illustration(
            owned.requested_by,
            owned.campaign_id,
            self._draft(owned),
            stored,
            provenance,
            asset_id=owned.asset_id,
            expected_current_version_id=owned.expected_current_version_id,
        )
        owned.status = CreativeGenerationStatus.SUCCEEDED
        owned.result_asset_id = version.asset_id
        owned.result_version_id = version.id
        owned.finished_at = datetime.now(UTC)

    async def finish_unsuccessfully(
        self, job_id: UUID, claim_token: UUID, code: str, *, outcome_unknown: bool = False
    ) -> None:
        await self.session.execute(
            update(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.id == job_id,
                CreativeGenerationJob.workspace_id == DEFAULT_WORKSPACE_ID,
                CreativeGenerationJob.status == CreativeGenerationStatus.RUNNING,
                CreativeGenerationJob.claim_token == claim_token,
            )
            .values(
                status=(
                    CreativeGenerationStatus.UNKNOWN
                    if outcome_unknown
                    else CreativeGenerationStatus.FAILED
                ),
                error_code=code,
                finished_at=func.now(),
            )
        )

    async def recover_stale_jobs(self) -> None:
        cutoff = datetime.now(UTC)
        await self.session.execute(
            update(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.status == CreativeGenerationStatus.PENDING,
                CreativeGenerationJob.created_at < cutoff - PENDING_LIFETIME,
            )
            .values(
                status=CreativeGenerationStatus.FAILED,
                error_code="CREATIVE_GENERATION_QUEUE_EXPIRED",
                finished_at=func.now(),
            )
        )
        await self.session.execute(
            update(CreativeGenerationJob)
            .where(
                CreativeGenerationJob.status == CreativeGenerationStatus.RUNNING,
                CreativeGenerationJob.started_at < cutoff - RUNNING_LIFETIME,
            )
            .values(
                status=CreativeGenerationStatus.UNKNOWN,
                error_code="CREATIVE_GENERATION_WORKER_LOST",
                finished_at=func.now(),
            )
        )

    @staticmethod
    def _draft(job: CreativeGenerationJob) -> IllustrationDraft:
        return IllustrationDraft.model_validate(
            {**job.draft, "source_version_id": job.source_version_id}
        )

    @staticmethod
    def _dto(job: CreativeGenerationJob) -> GenerationJob:
        return GenerationJob(
            id=job.id,
            status=CreativeGenerationStatus(job.status),
            asset_id=job.result_asset_id or job.asset_id,
            version_id=job.result_version_id,
            error_code=job.error_code,
        )
