from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.models.publication import (
    LegacyExternalPlacement,
    LegacyPublicationAttempt,
    Publication,
    PublicationClaimEvent,
    PublicationStatus,
)
from app.models.publication_metrics import MetricsSource, PublicationMetricsSnapshot
from app.tests import test_legacy_publication_attempt_schema as attempt_fixtures

pytestmark = pytest.mark.integration
approved_publication = attempt_fixtures.approved_publication
issued_claim = attempt_fixtures.issued_claim


async def test_0033_preserves_existing_success_and_observations_without_backfill(
    db_session, issued_claim
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    directory = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
    migration = directory.get_revision("20261123_0033").module
    attempts = LegacyPublicationAttempt.__table__
    placements = LegacyExternalPlacement.__table__
    snapshots = PublicationMetricsSnapshot.__table__
    legacy_columns = [col for col in snapshots.c if col.name != "external_placement_id"]
    read_snapshots = select(*legacy_columns).order_by(snapshots.c.id)
    publication = Publication.__table__
    read_publication = publication.select().where(publication.c.id == issued_claim.publication_id)

    def rehearse(connection):
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            newer_revisions = directory.iterate_revisions(
                context.get_current_revision(), migration.down_revision
            )
            for newer in newer_revisions:
                newer.module.downgrade()
                context.stamp(directory, newer.down_revision)
            assert context.get_current_revision() == "20261123_0032"
            assert not inspect(connection).has_table(placements.name)
            assert "external_placement_id" not in {
                col["name"] for col in inspect(connection).get_columns(snapshots.name)
            }

            connection.execute(
                attempts.insert().values(
                    claim_event_id=issued_claim.id,
                    status="SUCCEEDED",
                    finished_at=func.now(),
                    claimed_target_id="@legacy",
                    provider_target_id="-123",
                    external_id="42",
                )
            )
            connection.execute(
                publication.update()
                .where(publication.c.id == issued_claim.publication_id)
                .values(
                    status=PublicationStatus.PUBLISHED,
                    provider_target_id="-123",
                    external_id="42",
                    published_at=func.now(),
                )
            )
            for source, provider in (
                (MetricsSource.MANUAL, None),
                (MetricsSource.PROVIDER, "telegram_mtproto"),
            ):
                connection.execute(
                    snapshots.insert().values(
                        publication_id=issued_claim.publication_id,
                        channel=issued_claim.channel,
                        observed_at=func.now(),
                        source=source,
                        provider=provider,
                        views=0,
                    )
                )
            old_attempt = connection.execute(attempts.select()).one()
            old_publication = connection.execute(read_publication).one()
            old_snapshots = connection.execute(read_snapshots).all()

            migration.upgrade()
            context.stamp(directory, migration.revision)
            assert context.get_current_revision() == "20261123_0033"
            assert connection.scalar(select(func.count()).select_from(placements)) == 0
            assert connection.scalars(select(snapshots.c.external_placement_id)).all() == [None] * 2
            assert connection.execute(read_snapshots).all() == old_snapshots
            assert connection.execute(attempts.select()).one() == old_attempt
            assert connection.execute(read_publication).one() == old_publication

            # Legacy evidence with no placement is also safe to keep through a downgrade.
            migration.downgrade()
            context.stamp(directory, migration.down_revision)
            assert not inspect(connection).has_table(placements.name)
            assert connection.execute(read_snapshots).all() == old_snapshots
            assert connection.execute(attempts.select()).one() == old_attempt
            assert connection.execute(read_publication).one() == old_publication

    connection = await db_session.connection()
    savepoint = await connection.begin_nested()
    try:
        await connection.run_sync(rehearse)
    finally:
        await savepoint.rollback()


async def test_placement_identity_and_evidence_survive_collisions_and_downgrade(
    db_session, issued_claim
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    attempts = LegacyPublicationAttempt.__table__
    placements = LegacyExternalPlacement.__table__
    claims = PublicationClaimEvent.__table__
    metrics = PublicationMetricsSnapshot.__table__
    now = datetime.now(UTC)
    receipt = dict(
        status="SUCCEEDED", finished_at=func.now(), external_id="42", provider_target_id="-123"
    )
    await db_session.execute(attempts.insert().values(claim_event_id=issued_claim.id, **receipt))
    remote_object = dict(
        channel=issued_claim.channel, external_id="42", provider_target_id="-123", confirmed_at=now
    )
    placement_id = await db_session.scalar(
        placements.insert()
        .values(claim_event_id=issued_claim.id, **remote_object)
        .returning(placements.c.id)
    )
    second_claim_id = uuid4()
    await db_session.execute(
        claims.insert().values(
            id=second_claim_id,
            publication_id=issued_claim.publication_id,
            content_version_id=issued_claim.content_version_id,
            claim_digest="1" * 64,
            channel=issued_claim.channel,
            event_type=issued_claim.event_type,
            schema_version=issued_claim.schema_version,
            delivery_owner=issued_claim.delivery_owner,
        )
    )
    await db_session.execute(attempts.insert().values(claim_event_id=second_claim_id, **receipt))
    with pytest.raises(IntegrityError) as duplicate:
        async with db_session.begin_nested():
            await db_session.execute(
                placements.insert().values(claim_event_id=second_claim_id, **remote_object)
            )
    assert duplicate.value.orig.sqlstate == "23505"

    with pytest.raises(IntegrityError) as cascade:
        async with db_session.begin_nested():
            await db_session.execute(claims.delete().where(claims.c.id == issued_claim.id))
    assert cascade.value.orig.sqlstate == "23503"

    def downgrade(connection):
        directory = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
        migration = directory.get_revision("20261123_0033").module
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="Refusing to drop recorded legacy external placements"):
        await connection.run_sync(downgrade)
    assert await db_session.scalar(select(placements.c.id)) == placement_id

    observation = dict(
        publication_id=issued_claim.publication_id,
        external_placement_id=placement_id,
        channel=issued_claim.channel,
        observed_at=now,
        views=0,
    )
    with pytest.raises(IntegrityError) as manual:
        async with db_session.begin_nested():
            await db_session.execute(
                metrics.insert().values(source=MetricsSource.MANUAL, **observation)
            )
    assert manual.value.orig.sqlstate == "23514"

    await db_session.execute(
        metrics.insert().values(
            source=MetricsSource.PROVIDER, provider="telegram_mtproto", **observation
        )
    )
    with pytest.raises(IntegrityError) as referenced:
        async with db_session.begin_nested():
            await db_session.execute(placements.delete().where(placements.c.id == placement_id))
    assert referenced.value.orig.sqlstate == "23503"

    assert await db_session.scalar(select(metrics.c.external_placement_id)) == placement_id
