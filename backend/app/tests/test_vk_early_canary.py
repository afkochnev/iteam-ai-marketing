from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.telegram import ProviderPublicationResult
from app.models.activity import ActivityLog
from app.models.publication import Publication, PublicationStatus
from app.services.publication_service import PublicationService
from app.tests.test_plan_publication_scheduling import plan_post


async def scheduled_canary(session, monkeypatch, *, overridden=False):
    user, _, post, version, plan_item = await plan_post(session)
    service = PublicationService(session)
    if overridden:
        plan_item.scheduled_at = datetime.now(UTC) - timedelta(days=2)
        await session.commit()
        created = await service.reschedule_overdue_plan_content(
            post.id, datetime.now(UTC) + timedelta(days=1), user, reason="Reviewed reschedule"
        )
    else:
        created = await service.schedule_content(post.id, user)
    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    monkeypatch.setattr(settings, "vk_canary_publication_id", created.id)
    return user, service, created, plan_item, version


@pytest.mark.parametrize("overridden", [False, True])
async def test_exact_human_canary_can_publish_early_without_mutating_plan(
    db_session, monkeypatch, overridden
):
    user, service, created, plan, version = await scheduled_canary(
        db_session, monkeypatch, overridden=overridden
    )
    original_schedule, plan_schedule = created.scheduled_at, plan.scheduled_at
    claimed = await service.claim_for_publish(created.id, user)
    row = await db_session.get(Publication, created.id)
    assert claimed.status is PublicationStatus.PUBLISHING
    assert row.scheduled_at == original_schedule and plan.scheduled_at == plan_schedule
    token = row.execution_token
    event = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-canary-early:{created.id}"
        )
    )
    assert event.user_id == user.id
    assert event.metadata_["execution_token"] == token
    assert event.metadata_["content_version_id"] == str(version.id)

    class Provider:
        calls = 0

        async def publish(self, *, text, chat_id):
            self.calls += 1
            assert text == version.content and chat_id == "-150574411"
            return ProviderPublicationResult(
                "1234", None, datetime.now(UTC), provider_target_id="-150574411"
            )

    provider = Provider()
    result = await service.execute_vk(created.id, provider, execution_token=token)
    assert result.status is PublicationStatus.PUBLISHED
    assert result.provider_target_id == "-150574411" and result.external_id == "1234"
    assert provider.calls == 1
    assert await service.execute_vk(created.id, provider, execution_token=token) is None
    assert provider.calls == 1


@pytest.mark.parametrize(
    "blocked", ["scheduler", "no_gate", "other_gate", "disabled", "unapproved"]
)
async def test_future_publication_is_not_implicitly_authorized(db_session, monkeypatch, blocked):
    user, service, created, _, _ = await scheduled_canary(db_session, monkeypatch)
    if blocked == "no_gate":
        monkeypatch.setattr(settings, "vk_canary_publication_id", None)
    elif blocked == "other_gate":
        monkeypatch.setattr(settings, "vk_canary_publication_id", uuid4())
    elif blocked == "disabled":
        monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    elif blocked == "unapproved":
        row = await db_session.get(Publication, created.id)
        row.approved_for_publish_at = None
        await db_session.commit()
    with pytest.raises(AppError):
        await service.claim_for_publish(created.id, None if blocked == "scheduler" else user)
    row = await db_session.get(Publication, created.id)
    assert row.status is PublicationStatus.SCHEDULED and row.execution_token is None
    assert row.provider_target_id is None
    assert (
        await db_session.scalar(
            select(ActivityLog).where(
                ActivityLog.operation_key == f"publication-canary-early:{created.id}"
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "field",
    [
        "execution_token",
        "content_version_id",
        "scheduled_at",
        "provider_target_id",
        "channel",
        "user_id",
    ],
)
async def test_worker_requires_exact_durable_early_intent(db_session, monkeypatch, field):
    user, service, created, _, _ = await scheduled_canary(db_session, monkeypatch)
    await service.claim_for_publish(created.id, user)
    row = await db_session.get(Publication, created.id)
    token = row.execution_token
    event = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-canary-early:{created.id}"
        )
    )
    if field == "user_id":
        event.user_id = None
    else:
        event.metadata_ = {**event.metadata_, field: "mismatch"}
    await db_session.commit()

    class NeverProvider:
        async def publish(self, **kwargs):
            pytest.fail("invalid early authorization must never call wall.post")

    assert await service.execute_vk(created.id, NeverProvider(), execution_token=token) is None
