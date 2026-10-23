import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError
from app.integrations.telegram import ProviderPublicationResult, VKProvider, VKProviderError
from app.models.activity import ActivityLog
from app.models.approval import Approval
from app.models.publication import Publication, PublicationStatus
from app.tests.test_vk_early_canary import scheduled_canary


async def failed_canary(session, monkeypatch):
    user, service, created, plan, version = await scheduled_canary(session, monkeypatch)
    approval = await session.scalar(
        select(Approval).where(Approval.object_id == created.content_item_id)
    )
    approval.subject_snapshot = {
        **approval.subject_snapshot,
        "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
    }
    await session.commit()
    await service.claim_for_publish(created.id, user)
    row = await session.get(Publication, created.id)
    old_token = row.execution_token

    class Denied:
        async def publish(self, **kwargs):
            raise VKProviderError("VK_PERMISSION_ERROR", "VK access was denied.")

    await service.execute_vk(created.id, Denied(), execution_token=old_token)
    # Fixtures generate random identifiers; substitute the one-off authorization constants.
    for key, value in {
        "CANARY_ID": created.id,
        "CANARY_VERSION_ID": version.id,
        "RETRY_KEY": f"publication-canary-retry:{created.id}",
        "CONSUMED_KEY": f"publication-canary-retry:{created.id}:consumed",
    }.items():
        monkeypatch.setattr("app.services.publication_service." + key, value)
    return user, service, row, plan, version, old_token


@pytest.mark.parametrize("outcome", ["success", "permission", "rate_limit", "timeout"])
async def test_retry_is_one_use_audited_and_never_replayed(db_session, monkeypatch, outcome):
    user, service, row, plan, version, old_token = await failed_canary(db_session, monkeypatch)
    schedule, plan_schedule = row.scheduled_at, plan.scheduled_at
    await service.claim_canary_retry(row.id, user)
    token = row.execution_token
    assert token and token != old_token and row.retry_count == 1
    intent = await db_session.scalar(
        select(ActivityLog).where(ActivityLog.operation_key == f"publication-canary-retry:{row.id}")
    )
    assert intent.user_id == user.id
    assert intent.metadata_ == {
        **service._early_canary_snapshot(row),
        "previous_failure_code": "VK_PERMISSION_ERROR",
    }
    assert row.scheduled_at == schedule and plan.scheduled_at == plan_schedule
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user)
    with pytest.raises(AppError):
        await service.claim_for_publish(row.id, user)

    class Provider:
        calls = 0

        async def publish(self, *, text, chat_id):
            self.calls += 1
            assert text == version.content and chat_id == "-150574411"
            if outcome == "permission":
                raise VKProviderError(
                    "VK_PERMISSION_ERROR",
                    "VK access was denied.",
                    error_code=15,
                    error_subcode=1133,
                )
            if outcome == "rate_limit":
                raise VKProviderError("VK_RATE_LIMIT", "VK rate limit reached.", retryable=True)
            if outcome == "timeout":
                raise VKProviderError("VK_PROVIDER_TIMEOUT", "Unknown delivery.", ambiguous=True)
            return ProviderPublicationResult("123", None, datetime.now(UTC), "-150574411")

    provider = Provider()
    assert await service.execute_vk(row.id, provider, execution_token=old_token) is None
    result = await service.execute_vk(row.id, provider, execution_token=token)
    assert result is not None and provider.calls == 1
    assert result.retry_count == 1 and not result.retry_allowed
    assert result.status is (
        PublicationStatus.PUBLISHED if outcome == "success" else PublicationStatus.FAILED
    )
    if outcome == "permission":
        event = await db_session.scalar(
            select(ActivityLog).where(ActivityLog.operation_key == f"publication-failed:{row.id}:1")
        )
        assert event.metadata_["vk_error_code"] == 15
        assert event.metadata_["vk_error_subcode"] == 1133
    assert await service.execute_vk(row.id, provider, execution_token=token) is None
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user)
    assert provider.calls == 1
    assert await db_session.scalar(
        select(ActivityLog.id).where(
            ActivityLog.operation_key == f"publication-canary-retry:{row.id}:consumed"
        )
    )


@pytest.mark.parametrize(
    "blocked",
    [
        "wrong_id",
        "wrong_version",
        "disabled",
        "no_gate",
        "other_gate",
        "wrong_owner",
        "wrong_target",
        "external_id",
        "published_at",
        "ambiguous",
        "unapproved",
        "wrong_status",
        "retry_count",
        "changed_content",
        "missing_hash",
    ],
)
async def test_retry_rejects_invalid_preconditions(db_session, monkeypatch, blocked):
    user, service, row, _, version, _ = await failed_canary(db_session, monkeypatch)
    if blocked in {"wrong_id", "wrong_version"}:
        monkeypatch.setattr(
            "app.services.publication_service."
            + ("CANARY_ID" if blocked == "wrong_id" else "CANARY_VERSION_ID"),
            uuid4(),
        )
    elif blocked == "disabled":
        monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    elif blocked in {"no_gate", "other_gate"}:
        monkeypatch.setattr(
            settings, "vk_canary_publication_id", None if blocked == "no_gate" else uuid4()
        )
    elif blocked == "wrong_owner":
        monkeypatch.setattr(settings, "vk_owner_id", -123)
    elif blocked == "wrong_target":
        row.provider_target_id = "-123"
    elif blocked == "external_id":
        row.external_id = "123"
    elif blocked == "published_at":
        row.published_at = datetime.now(UTC)
    elif blocked == "ambiguous":
        row.failure_code = "VK_RECONCILIATION_REQUIRED"
    elif blocked == "unapproved":
        row.approved_for_publish_at = None
    elif blocked == "wrong_status":
        row.status = PublicationStatus.SCHEDULED
    elif blocked == "retry_count":
        row.retry_count = 1
    elif blocked == "changed_content":
        version.content += " changed"
    elif blocked == "missing_hash":
        approval = await db_session.scalar(
            select(Approval).where(Approval.object_id == row.content_item_id)
        )
        approval.subject_snapshot = {
            k: v for k, v in approval.subject_snapshot.items() if k != "content_hash"
        }
    await db_session.commit()
    with pytest.raises(AppError):
        await service.claim_canary_retry(row.id, user)
    assert not await service.has_canary_retry(row.id)


@pytest.mark.parametrize("tamper", ["intent", "disabled", "gate", "target"])
async def test_worker_revalidates_retry_intent(db_session, monkeypatch, tamper):
    user, service, row, _, _, _ = await failed_canary(db_session, monkeypatch)
    await service.claim_canary_retry(row.id, user)
    if tamper == "intent":
        event = await db_session.scalar(
            select(ActivityLog).where(
                ActivityLog.operation_key == f"publication-canary-retry:{row.id}"
            )
        )
        event.metadata_ = {**event.metadata_, "execution_token": "stale"}
    elif tamper == "disabled":
        monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    elif tamper == "gate":
        monkeypatch.setattr(settings, "vk_canary_publication_id", None)
    elif tamper == "target":
        row.provider_target_id = "-123"
    await db_session.commit()

    class Never:
        async def publish(self, **kwargs):
            pytest.fail("invalid retry must not contact VK")

    assert await service.execute_vk(row.id, Never(), execution_token=row.execution_token) is None


@pytest.mark.parametrize("code", [5, 27, 7, 15, 6])
async def test_single_attempt_provider_never_retries_wall_post(monkeypatch, code):
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    monkeypatch.setattr(settings, "vk_access_token", None)

    class Store:
        async def access_token(self, *, force_refresh=False):
            assert not force_refresh
            return "sentinel-secret"

    provider = VKProvider(credential_store=Store(), single_attempt=True)
    calls = []

    async def send(**kwargs):
        calls.append(True)
        return {"error": {"error_code": code, "error_msg": "sentinel-secret"}}

    monkeypatch.setattr(provider, "_send", send)
    with pytest.raises(VKProviderError) as error:
        await provider.publish(text="test", chat_id="-150574411")
    assert len(calls) == 1
    assert "sentinel-secret" not in str(error.value)


async def test_concurrent_redelivery_consumes_provider_request_once(db_session, monkeypatch):
    from app.core.database import async_session_factory
    from app.services.publication_service import PublicationService

    user, service, row, _, _, _ = await failed_canary(db_session, monkeypatch)
    await service.claim_canary_retry(row.id, user)
    publication_id, token = row.id, row.execution_token
    await db_session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    class Blocking:
        calls = 0

        async def publish(self, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return ProviderPublicationResult("123", None, datetime.now(UTC), "-150574411")

    provider = Blocking()
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


async def test_api_enqueues_exact_token_once_and_blocks_generic_retry(
    client, db_session, monkeypatch
):
    from app.api.dependencies import get_current_user
    from app.main import app
    from app.workers.vk_worker import publish_vk_publication

    user, service, row, _, _, _ = await failed_canary(db_session, monkeypatch)

    async def current_user():
        return user

    app.dependency_overrides[get_current_user] = current_user
    calls = []
    monkeypatch.setattr(
        publish_vk_publication, "apply_async", lambda **kwargs: calls.append(kwargs)
    )
    response = await client.post(f"/api/v1/publications/{row.id}/canary-retry")
    assert response.status_code == 202
    assert len(calls) == 1
    assert calls[0] == {"args": [str(row.id), row.execution_token], "queue": "publication"}
    duplicate = await client.post(f"/api/v1/publications/{row.id}/canary-retry")
    assert duplicate.status_code == 409 and len(calls) == 1
    row.status = PublicationStatus.FAILED
    row.failure_code = "VK_RATE_LIMIT"
    await db_session.commit()
    generic = await client.post(f"/api/v1/publications/{row.id}/retry")
    assert generic.status_code == 409 and len(calls) == 1
