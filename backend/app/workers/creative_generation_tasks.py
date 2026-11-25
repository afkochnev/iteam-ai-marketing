import asyncio
import logging
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import create_worker_session_factory
from app.core.errors import AppError
from app.integrations.creative_media import (
    CreativeImageGenerationError,
    CreativeImageSize,
    CreativeImageValidationError,
    CreativeMediaStorageError,
    generate_creative_image,
    store_creative_image,
)
from app.schemas.content_asset import IllustrationProvenance
from app.services.creative_generation_service import CreativeGenerationService
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


def dispatch_creative_generation(job_id: UUID) -> None:
    generate_campaign_illustration.apply_async(args=[str(job_id)], queue="ai", retry=False)


@celery_app.task(  # type: ignore[misc]
    name="generate_campaign_illustration", ignore_result=True, time_limit=270
)
def generate_campaign_illustration(job_id: str) -> None:
    asyncio.run(_run_in_worker_loop(UUID(job_id)))


@celery_app.task(name="recover_stale_creative_generation", ignore_result=True)  # type: ignore[misc]
def recover_stale_creative_generation() -> None:
    asyncio.run(_recover_in_worker_loop())


async def _run_in_worker_loop(job_id: UUID) -> None:
    engine, factory = create_worker_session_factory()
    try:
        await run_creative_generation(job_id, factory)
    finally:
        await engine.dispose()


async def _recover_in_worker_loop() -> None:
    engine, factory = create_worker_session_factory()
    try:
        async with factory.begin() as session:
            await CreativeGenerationService(session).recover_stale_jobs()
    finally:
        await engine.dispose()


async def run_creative_generation(job_id: UUID, factory: SessionFactory) -> None:
    async with factory.begin() as session:
        service = CreativeGenerationService(session)
        job = await service.claim(job_id)
        if job is None or job.claim_token is None:
            return
        token = job.claim_token
        try:
            await service.validate_claim(job)
        except AppError as error:
            await service.finish_unsuccessfully(job_id, token, error.code)
            return
        prompt, model, size = job.prompt, job.model, cast(CreativeImageSize, job.size)

    # The committed claim is consumed before any I/O. A duplicate delivery or
    # late response cannot authorize another call or complete a closed job.
    try:
        generated = await generate_creative_image(prompt=prompt, model=model, size=size)
    except CreativeImageGenerationError as error:
        await _finish_unsuccessfully(factory, job_id, token, error.code, error.outcome_unknown)
        return
    except Exception:
        logger.error("Unexpected creative provider failure for job %s", job_id)
        await _finish_unsuccessfully(
            factory, job_id, token, "CREATIVE_GENERATION_PROVIDER_UNKNOWN", True
        )
        return

    try:
        async with asyncio.timeout(60):
            stored = await store_creative_image(generated.content)
            provenance = IllustrationProvenance(
                source="GENERATED",
                model=model,
                prompt=prompt,
                provider_request_id=generated.provider_request_id,
                generation_size=size,
            )
            async with factory.begin() as session:
                await CreativeGenerationService(session).finish(job_id, token, stored, provenance)
    except (CreativeImageValidationError, CreativeMediaStorageError, AppError) as error:
        await _finish_unsuccessfully(factory, job_id, token, error.code)
    except Exception:
        logger.error("Unexpected creative result persistence failure for job %s", job_id)
        await _finish_unsuccessfully(
            factory, job_id, token, "CREATIVE_GENERATION_PERSISTENCE_UNKNOWN", True
        )


async def _finish_unsuccessfully(
    factory: SessionFactory,
    job_id: UUID,
    token: UUID,
    code: str,
    outcome_unknown: bool = False,
) -> None:
    async with factory.begin() as session:
        await CreativeGenerationService(session).finish_unsuccessfully(
            job_id, token, code, outcome_unknown=outcome_unknown
        )
