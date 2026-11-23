from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.activity import ActivityLog
from app.models.publication import LegacyPublicationAttempt, PublicationClaimEvent
from app.services.publication_service import PublicationService
from app.tests import test_publication_claim_events as claim_fixtures

pytestmark = pytest.mark.integration
approved_publication = claim_fixtures.approved_publication


@pytest.fixture
async def issued_claim(db_session, approved_publication):
    user, publication = approved_publication
    await PublicationService(db_session).claim_for_publish(publication.id, user)
    query = select(PublicationClaimEvent).filter_by(publication_id=publication.id)
    return (await db_session.scalars(query)).one()


async def test_0032_preserves_claim_and_audit_without_inventing_a_provider_attempt(
    db_session, issued_claim
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    directory = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
    migration = directory.get_revision("20261123_0032").module

    event = issued_claim

    def rehearse(connection):
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
            context.stamp(directory, migration.down_revision)
            assert context.get_current_revision() == "20261123_0031"

            claims = PublicationClaimEvent.__table__
            read_claim = claims.select().where(claims.c.id == event.id)
            before = dict(connection.execute(read_claim).mappings().one())
            start_key = f"publication-started:{event.publication_id}:{event.claim_digest}"
            connection.execute(
                ActivityLog.__table__.insert().values(
                    event_type="PUBLICATION_STARTED", operation_key=start_key
                )
            )
            read_start = select(ActivityLog.id).where(ActivityLog.operation_key == start_key)
            original_start_id = connection.execute(read_start).scalar_one()

            migration.upgrade()
            context.stamp(directory, migration.revision)
            assert context.get_current_revision() == "20261123_0032"
            assert dict(connection.execute(read_claim).mappings().one()) == before
            assert connection.execute(read_start).scalar_one() == original_start_id
            attempts = LegacyPublicationAttempt.__table__
            assert connection.scalar(select(func.count()).select_from(attempts)) == 0

            connection.execute(attempts.insert().values(claim_event_id=event.id, status="STARTED"))
            with pytest.raises(RuntimeError, match="Refusing to drop recorded"):
                migration.downgrade()
            assert connection.execute(select(attempts.c.status)).scalar_one() == "STARTED"

    connection = await db_session.connection()
    savepoint = await connection.begin_nested()
    try:
        await connection.run_sync(rehearse)
    finally:
        await savepoint.rollback()


async def test_storage_accepts_only_one_start_and_consistent_outcomes_per_claim(
    db_session, issued_claim
):
    attempts = LegacyPublicationAttempt.__table__
    start = attempts.insert().values(claim_event_id=issued_claim.id, status="STARTED")
    started_at = (await db_session.execute(start.returning(attempts.c.started_at))).scalar_one()
    with pytest.raises(IntegrityError) as duplicate:
        async with db_session.begin_nested():
            await db_session.execute(start)
    assert duplicate.value.orig.sqlstate == "23505"

    before_start = started_at - timedelta(seconds=1)
    change = attempts.update().where(attempts.c.claim_event_id == issued_claim.id)
    invalid_outcomes = (
        {"status": "SUCCEEDED", "finished_at": started_at, "external_id": " "},
        {"status": "FAILED", "finished_at": started_at, "failure_code": "raw provider message"},
        {"status": "UNKNOWN", "finished_at": started_at, "failure_code": "X", "external_id": "1"},
        {"status": "SUCCEEDED", "finished_at": before_start, "external_id": "1"},
    )
    for values in invalid_outcomes:
        with pytest.raises(IntegrityError) as invalid:
            async with db_session.begin_nested():
                await db_session.execute(change.values(**values))
        assert invalid.value.orig.sqlstate == "23514"
    assert await db_session.scalar(select(attempts.c.status)) == "STARTED"
