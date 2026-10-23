import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.telegram import ProviderPublicationResult, VKProviderError
from app.models.activity import ActivityLog
from app.models.publication import PublicationStatus
from app.services.publication_service import PublicationService
from app.tests.test_vk_canary_retry import failed_canary


async def old_history(session, publication_id):
    keys = [
        f"publication-canary-retry:{publication_id}",
        f"publication-canary-retry:{publication_id}:consumed",
        f"publication-failed:{publication_id}:1",
        f"publication-started:{publication_id}:canary-retry",
    ]
    rows = (
        await session.scalars(
            select(ActivityLog)
            .where(ActivityLog.operation_key.in_(keys))
            .order_by(ActivityLog.operation_key)
        )
    ).all()
    return json.dumps(
        [
            {
                "id": str(e.id),
                "key": e.operation_key,
                "event": e.event_type,
                "user": str(e.user_id),
                "metadata": e.metadata_,
            }
            for e in rows
        ],
        sort_keys=True,
    )


async def consumed_first_retry(session, monkeypatch):
    user, service, row, plan, version, _ = await failed_canary(session, monkeypatch)
    await service.claim_canary_retry(row.id, user)

    class Denied:
        async def publish(self, **kwargs):
            raise VKProviderError("VK_PERMISSION_ERROR", "VK access was denied.", error_code=15)

    await service.execute_vk(row.id, Denied(), execution_token=row.execution_token)
    for key, value in {
        "RETRY_V2_KEY": f"publication-canary-retry-v2:{row.id}",
        "CONSUMED_V2_KEY": f"publication-canary-retry-v2:{row.id}:consumed",
    }.items():
        monkeypatch.setattr("app.services.publication_service." + key, value)
    return user, service, row, plan, version


@pytest.mark.parametrize("outcome", ["success", "permission", "rate_limit", "ambiguous"])
async def test_new_authorization_preserves_old_and_executes_once(db_session, monkeypatch, outcome):
    user, service, row, plan, version = await consumed_first_retry(db_session, monkeypatch)
    old = await old_history(db_session, row.id)
    schedule, plan_schedule, stale = row.scheduled_at, plan.scheduled_at, row.execution_token
    await service.claim_canary_retry(row.id, user, new_authorization=True)
    token = row.execution_token
    assert row.retry_count == 2 and token != stale
    assert row.scheduled_at == schedule and plan.scheduled_at == plan_schedule
    intent = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-canary-retry-v2:{row.id}"
        )
    )
    assert intent.event_type == "PUBLICATION_CANARY_RETRY_V2_REQUESTED"
    assert intent.user_id == user.id
    assert intent.metadata_["authorization_id"] == "credential-store-fixed-v1"
    assert intent.metadata_["previous_authorization_key"] == f"publication-canary-retry:{row.id}"
    assert intent.metadata_["content_version_id"] == str(version.id)
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user, new_authorization=True)
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user)

    class Provider:
        calls = 0

        async def publish(self, *, text, chat_id):
            self.calls += 1
            assert text == version.content and chat_id == "-150574411"
            if outcome == "permission":
                raise VKProviderError(
                    "VK_PERMISSION_ERROR", "Denied.", error_code=15, error_subcode=1133
                )
            if outcome == "rate_limit":
                raise VKProviderError("VK_RATE_LIMIT", "Limited.", retryable=True)
            if outcome == "ambiguous":
                raise VKProviderError("VK_PROVIDER_TIMEOUT", "Uncertain.", ambiguous=True)
            return ProviderPublicationResult("123", None, datetime.now(UTC), "-150574411")

    provider = Provider()
    assert await service.execute_vk(row.id, provider, execution_token=stale) is None
    result = await service.execute_vk(row.id, provider, execution_token=token)
    assert result is not None and result.retry_count == 2 and not result.retry_allowed
    assert result.status is (
        PublicationStatus.PUBLISHED if outcome == "success" else PublicationStatus.FAILED
    )
    assert await service.execute_vk(row.id, provider, execution_token=token) is None
    assert provider.calls == 1
    if outcome == "permission":
        failure = await db_session.scalar(
            select(ActivityLog).where(ActivityLog.operation_key == f"publication-failed:{row.id}:2")
        )
        assert failure.metadata_["vk_error_code"] == 15
        assert failure.metadata_["vk_error_subcode"] == 1133
    assert old == await old_history(db_session, row.id)
    consumed = await db_session.scalar(
        select(ActivityLog).where(
            ActivityLog.operation_key == f"publication-canary-retry-v2:{row.id}:consumed"
        )
    )
    assert consumed.event_type == "PUBLICATION_CANARY_RETRY_V2_CONSUMED"
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user, new_authorization=True)
    with pytest.raises(AppError):
        await service.claim_for_publish(row.id, user)


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_prior",
        "prior_consumed",
        "prior_failure",
        "disabled",
        "gate",
        "version",
        "target",
        "delivery",
        "hash",
        "count",
    ],
)
async def test_new_authorization_requires_intact_prior_and_current_guards(
    db_session, monkeypatch, invalid
):
    user, service, row, _, version = await consumed_first_retry(db_session, monkeypatch)
    if invalid in {"missing_prior", "prior_consumed", "prior_failure"}:
        key = (
            f"publication-canary-retry:{row.id}"
            if invalid == "missing_prior"
            else f"publication-canary-retry:{row.id}:consumed"
            if invalid == "prior_consumed"
            else f"publication-failed:{row.id}:1"
        )
        event = await db_session.scalar(select(ActivityLog).where(ActivityLog.operation_key == key))
        event.event_type = "INVALID_TEST_AUDIT"
    elif invalid == "disabled":
        monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    elif invalid == "gate":
        monkeypatch.setattr(settings, "vk_canary_publication_id", None)
    elif invalid == "version":
        monkeypatch.setattr("app.services.publication_service.CANARY_VERSION_ID", uuid4())
    elif invalid == "target":
        row.provider_target_id = "-123"
    elif invalid == "delivery":
        row.external_id = "123"
    elif invalid == "hash":
        version.content += "changed"
    elif invalid == "count":
        row.retry_count = 0
    await db_session.commit()
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user, new_authorization=True)
    assert (
        await db_session.scalar(
            select(ActivityLog.id).where(
                ActivityLog.operation_key == f"publication-canary-retry-v2:{row.id}"
            )
        )
        is None
    )


async def test_new_endpoint_enqueues_only_once_and_old_endpoint_remains_consumed(
    client, db_session, monkeypatch
):
    from app.api.dependencies import get_current_user
    from app.main import app
    from app.workers.vk_worker import publish_vk_publication

    user, service, row, _, _ = await consumed_first_retry(db_session, monkeypatch)
    old = await old_history(db_session, row.id)

    async def current_user():
        return user

    app.dependency_overrides[get_current_user] = current_user
    calls = []
    monkeypatch.setattr(publish_vk_publication, "apply_async", lambda **kw: calls.append(kw))
    assert (await client.post(f"/api/v1/publications/{row.id}/canary-retry")).status_code == 409
    assert not calls
    assert (await client.post(f"/api/v1/publications/{row.id}/canary-retry-v2")).status_code == 202
    assert calls == [{"args": [str(row.id), row.execution_token], "queue": "publication"}]
    assert (await client.post(f"/api/v1/publications/{row.id}/canary-retry-v2")).status_code == 409
    assert len(calls) == 1 and old == await old_history(db_session, row.id)


async def test_concurrent_new_worker_redelivery_cannot_send_twice(db_session, monkeypatch):
    from app.core.database import async_session_factory

    user, service, row, _, _ = await consumed_first_retry(db_session, monkeypatch)
    await service.claim_canary_retry(row.id, user, new_authorization=True)
    publication_id, token = row.id, row.execution_token
    await db_session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    class Provider:
        calls = 0

        async def publish(self, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return ProviderPublicationResult("123", None, datetime.now(UTC), "-150574411")

    provider = Provider()
    async with async_session_factory() as first, async_session_factory() as second:
        task = asyncio.create_task(
            PublicationService(first).execute_vk(publication_id, provider, execution_token=token)
        )
        try:
            await asyncio.wait_for(started.wait(), 10)
            assert (
                await PublicationService(second).execute_vk(
                    publication_id, provider, execution_token=token
                )
                is None
            )
            await second.rollback()
        finally:
            release.set()
        await task
    assert provider.calls == 1


@pytest.mark.parametrize("tamper", ["authorization", "due_schedule", "disabled", "gate"])
async def test_worker_requires_new_snapshot_and_gate_before_provider(
    db_session, monkeypatch, tamper
):
    from datetime import timedelta

    user, service, row, _, _ = await consumed_first_retry(db_session, monkeypatch)
    await service.claim_canary_retry(row.id, user, new_authorization=True)
    if tamper == "authorization":
        event = await db_session.scalar(
            select(ActivityLog).where(
                ActivityLog.operation_key == f"publication-canary-retry-v2:{row.id}"
            )
        )
        event.metadata_ = {**event.metadata_, "authorization_id": "wrong"}
    elif tamper == "due_schedule":
        row.scheduled_at = datetime.now(UTC) - timedelta(seconds=1)
    elif tamper == "disabled":
        monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    else:
        monkeypatch.setattr(settings, "vk_canary_publication_id", None)
    await db_session.commit()

    class Never:
        async def publish(self, **kwargs):
            pytest.fail("invalid new authorization cannot publish")

    assert await service.execute_vk(row.id, Never(), execution_token=row.execution_token) is None
    assert (
        await db_session.scalar(
            select(ActivityLog.id).where(
                ActivityLog.operation_key == f"publication-canary-retry-v2:{row.id}:consumed"
            )
        )
        is None
    )
