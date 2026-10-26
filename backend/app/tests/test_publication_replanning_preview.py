from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.publication_replanning import ReplanningPreviewRequest
from app.services.publication_replanning import ScheduleSnapshot, preview_replanning

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def snapshot(**changes):
    return replace(
        ScheduleSnapshot(uuid4(), uuid4(), "VK", "SCHEDULED", NOW + timedelta(days=1), NOW),
        **changes,
    )


def request(row, **changes):
    return ReplanningPreviewRequest(
        changes=[
            dict(
                publication_id=row.publication_id,
                scheduled_at=NOW + timedelta(days=2),
                channel="VK",
                **changes,
            )
        ]
    )


def test_preview_uses_actual_schedule_and_does_not_mutate():
    row = snapshot()
    result = preview_replanning(request(row), [row], now=NOW)
    assert result.mode == "PREVIEW_ONLY"
    assert result.items[0].original_scheduled_at == row.scheduled_at
    assert result.items[0].proposed_scheduled_at == NOW + timedelta(days=2)
    assert row.scheduled_at == NOW + timedelta(days=1)
    assert not result.items[0].blockers


@pytest.mark.parametrize("status", ["PUBLISHED", "PUBLISHING", "CANCELLED", "FAILED"])
def test_terminal_and_inflight_are_blocked(status):
    row = snapshot(status=status)
    assert preview_replanning(request(row), [row], now=NOW).items[0].blockers


def test_channel_change_needs_new_content_approval():
    row = snapshot()
    payload = ReplanningPreviewRequest(
        changes=[
            dict(
                publication_id=row.publication_id,
                scheduled_at=NOW + timedelta(days=2),
                channel="TELEGRAM",
            )
        ]
    )
    result = preview_replanning(payload, [row], now=NOW)
    assert result.items[0].requires_content_approval
    assert result.items[0].blockers


def test_collision_with_unchanged_delivery_and_snapshot_changes():
    row = snapshot()
    other = snapshot(scheduled_at=NOW + timedelta(days=2))
    result = preview_replanning(request(row), [row, other], now=NOW)
    assert "В этом канале уже занят выбранный слот" in result.items[0].blockers
    altered = preview_replanning(
        request(row), [row, replace(other, updated_at=NOW + timedelta(seconds=1))], now=NOW
    )
    assert altered.snapshot_version != result.snapshot_version
    assert (
        preview_replanning(request(row), [other, row], now=NOW).snapshot_version
        == result.snapshot_version
    )


def test_duplicate_ids_naive_times_and_extra_fields_rejected():
    row = snapshot()
    change = request(row).changes[0].model_dump()
    for payload in [
        dict(changes=[change, change]),
        dict(changes=[dict(change, scheduled_at=datetime(2030, 1, 2))]),
        dict(changes=[change], execute=True),
    ]:
        with pytest.raises(ValidationError):
            ReplanningPreviewRequest(**payload)


def test_past_and_ambiguous_deliveries_are_blocked():
    row = snapshot(blocked=True)
    payload = ReplanningPreviewRequest(
        changes=[dict(publication_id=row.publication_id, scheduled_at=NOW, channel="VK")]
    )
    assert len(preview_replanning(payload, [row], now=NOW).items[0].blockers) == 2


async def test_api_reads_only_and_rejects_foreign_publication():
    from types import SimpleNamespace

    from app.api.publications import replanning_preview
    from app.core.errors import AppError
    from app.models.campaign import CampaignStatus
    from app.models.content import ContentChannel, ContentStatus
    from app.models.publication import PublicationStatus

    row = snapshot()
    publication = SimpleNamespace(
        id=row.publication_id,
        content_version_id=row.content_version_id,
        channel=ContentChannel.VK,
        status=PublicationStatus.SCHEDULED,
        scheduled_at=row.scheduled_at,
        updated_at=row.updated_at,
        execution_token=None,
        failure_code=None,
    )

    class ReadOnlySession:
        async def get(self, model, identity):
            return SimpleNamespace(status=CampaignStatus.ACTIVE)

        async def execute(self, query):
            return SimpleNamespace(
                all=lambda: [(publication, SimpleNamespace(status=ContentStatus.APPROVED))]
            )

        def __getattr__(self, name):
            raise AssertionError("Unexpected session operation: " + name)

    session = ReadOnlySession()
    response = await replanning_preview(uuid4(), request(row), None, session)
    assert response.mode == "PREVIEW_ONLY"
    foreign = snapshot()
    with pytest.raises(AppError) as error:
        await replanning_preview(uuid4(), request(foreign), None, session)
    assert error.value.code == "PUBLICATION_NOT_FOUND"
