from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.maintenance.backfill_reconciliation_history import (
    BackfillSpec,
    BackfillValidationError,
    apply_inspections,
    inspect_all,
)
from app.models.activity import ActivityLog
from app.models.publication import (
    Publication,
    PublicationReconciliation,
    PublicationStatus,
    ReconciliationDecision,
)
from app.tests.test_publications import _reconciliation_fixture


async def _fixture_with_evidence(
    session: AsyncSession, decision: ReconciliationDecision
) -> tuple[BackfillSpec, Publication, ActivityLog]:
    user, campaign, post, _version, publication = await _reconciliation_fixture(session)
    if decision is ReconciliationDecision.CONFIRMED_PUBLISHED:
        publication.status = PublicationStatus.PUBLISHED
        publication.failure_code = None
        publication.failure_message = None
        publication.external_id = "999999"
    await session.commit()
    created_at = datetime(2026, 9, 28, 8, 57, 6, 581249, tzinfo=UTC)
    if decision is ReconciliationDecision.CONFIRMED_PUBLISHED:
        created_at = datetime(2026, 9, 28, 8, 57, 12, 952077, tzinfo=UTC)
    event_type = (
        "PUBLICATION_RECONCILED_PUBLISHED"
        if decision is ReconciliationDecision.CONFIRMED_PUBLISHED
        else "PUBLICATION_RECONCILED_NOT_PUBLISHED"
    )
    activity = ActivityLog(
        id=uuid4(),
        event_type=event_type,
        operation_key=f"backfill-test:{publication.id}",
        campaign_id=campaign.id,
        user_id=user.id,
        content_item_id=post.id,
        metadata_={
            "publication_id": str(publication.id),
            **(
                {"external_id": "999999"}
                if decision is ReconciliationDecision.CONFIRMED_PUBLISHED
                else {}
            ),
        },
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(activity)
    await session.commit()
    spec = BackfillSpec(
        publication_id=publication.id,
        decision=decision,
        operator_user_id=user.id,
        activity_log_id=activity.id,
        activity_created_at=created_at,
        external_id="999999" if decision is ReconciliationDecision.CONFIRMED_PUBLISHED else None,
        expected_channel=publication.channel.value,
        expected_failure_code=(
            None
            if decision is ReconciliationDecision.CONFIRMED_PUBLISHED
            else "TELEGRAM_RECONCILIATION_REQUIRED"
        ),
        expected_publication_status=(
            PublicationStatus.PUBLISHED
            if decision is ReconciliationDecision.CONFIRMED_PUBLISHED
            else PublicationStatus.FAILED
        ),
    )
    return spec, publication, activity


@pytest.mark.integration
async def test_backfill_dry_run_is_read_only(db_session: AsyncSession) -> None:
    spec, publication, _activity = await _fixture_with_evidence(
        db_session, ReconciliationDecision.CONFIRMED_NOT_PUBLISHED
    )
    before = (publication.status, publication.external_id, publication.failure_code)
    inspections = await inspect_all(db_session, [spec])
    assert inspections[0].existing is None
    assert (publication.status, publication.external_id, publication.failure_code) == before
    assert await db_session.scalar(select(func.count()).select_from(PublicationReconciliation)) == 0


@pytest.mark.integration
async def test_backfill_apply_preserves_evidence_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    spec, publication, activity = await _fixture_with_evidence(
        db_session, ReconciliationDecision.CONFIRMED_PUBLISHED
    )
    before = (publication.status, publication.external_id, publication.published_at)
    inspections = await inspect_all(db_session, [spec])
    assert await apply_inspections(inspections, db_session) == 1
    row = await db_session.scalar(
        select(PublicationReconciliation).where(
            PublicationReconciliation.publication_id == publication.id
        )
    )
    assert row is not None
    assert row.created_at == activity.created_at
    assert row.external_id == "999999"
    assert row.external_url is None
    assert row.external_published_at is None
    assert row.note is None
    refreshed = await db_session.get(Publication, publication.id)
    assert refreshed is not None
    assert (refreshed.status, refreshed.external_id, refreshed.published_at) == before
    audit = list(
        (
            await db_session.scalars(
                select(ActivityLog).where(
                    ActivityLog.event_type == "PUBLICATION_RECONCILIATION_HISTORY_BACKFILLED"
                )
            )
        ).all()
    )
    assert len(audit) == 1
    assert audit[0].metadata_["original_activity_log_id"] == str(activity.id)

    second = await inspect_all(db_session, [spec])
    with pytest.raises(BackfillValidationError, match="already exists"):
        await apply_inspections(second, db_session)
    assert await db_session.scalar(select(func.count()).select_from(PublicationReconciliation)) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.event_type == "PUBLICATION_RECONCILIATION_HISTORY_BACKFILLED")
        )
        == 1
    )


@pytest.mark.integration
async def test_backfill_refuses_missing_or_wrong_evidence(db_session: AsyncSession) -> None:
    spec, _publication, activity = await _fixture_with_evidence(
        db_session, ReconciliationDecision.CONFIRMED_NOT_PUBLISHED
    )
    missing = spec.__class__(**{**spec.__dict__, "activity_log_id": uuid4()})
    with pytest.raises(BackfillValidationError, match="ActivityLog not found"):
        await inspect_all(db_session, [missing])
    activity.event_type = "PUBLICATION_CREATED"
    await db_session.commit()
    with pytest.raises(BackfillValidationError, match="Unexpected ActivityLog event"):
        await inspect_all(db_session, [spec])


@pytest.mark.integration
async def test_backfill_refuses_incompatible_publication_state(
    db_session: AsyncSession,
) -> None:
    spec, publication, _activity = await _fixture_with_evidence(
        db_session, ReconciliationDecision.CONFIRMED_NOT_PUBLISHED
    )
    publication.status = PublicationStatus.PUBLISHED
    await db_session.commit()
    with pytest.raises(BackfillValidationError, match="Unexpected status"):
        await inspect_all(db_session, [spec])
