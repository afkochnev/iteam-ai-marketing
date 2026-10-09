import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.error_monitoring import report_exception
from app.models.publication import Publication, PublicationStatus
from app.services.metrics_service import MetricsService
from app.workers.celery_app import celery_app


@celery_app.task(name="sync_publication_metrics")  # type: ignore[misc]
def sync_publication_metrics(publication_id: str) -> None:
    asyncio.run(_sync_one(UUID(publication_id)))


async def _sync_one(publication_id: UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await MetricsService(session).sync(publication_id)
    except Exception as error:
        report_exception(
            error, publication_id=str(publication_id), event="publication_metrics_sync_failed"
        )
        raise
    finally:
        await engine.dispose()


@celery_app.task(name="sync_recent_publication_metrics")  # type: ignore[misc]
def sync_recent_publication_metrics() -> None:
    if settings.metrics_sync_enabled:
        asyncio.run(_sync_recent())


async def _sync_recent() -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            cutoff = datetime.now(UTC) - timedelta(days=settings.metrics_lookback_days)
            publications = list(
                (
                    await session.scalars(
                        select(Publication)
                        .where(
                            Publication.status == PublicationStatus.PUBLISHED,
                            Publication.published_at >= cutoff,
                            Publication.external_id.is_not(None),
                            Publication.external_id != "",
                            Publication.channel.in_(
                                [
                                    channel
                                    for channel, enabled in [
                                        ("TELEGRAM", settings.telegram_metrics_enabled),
                                        ("VK", settings.vk_metrics_enabled),
                                    ]
                                    if enabled
                                ]
                            ),
                        )
                        .limit(100)
                    )
                ).all()
            )
        for publication in publications:
            sync_publication_metrics.apply_async(args=[str(publication.id)], queue="metrics")
    finally:
        await engine.dispose()


@celery_app.task(name="advance_marketing_experiments")  # type: ignore[misc]
def advance_marketing_experiments() -> None:
    asyncio.run(_advance_experiments())


async def _advance_experiments() -> None:
    from app.services.experiment_service import ExperimentService

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await ExperimentService(session).advance()
    except Exception as error:
        report_exception(error, event="marketing_experiment_lifecycle")
        raise
    finally:
        await engine.dispose()
