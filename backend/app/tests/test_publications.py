from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.errors import AppError
from app.integrations.telegram import (
    ProviderPublicationResult,
    TelegramProvider,
    TelegramProviderError,
)
from app.main import app
from app.models.activity import ActivityLog
from app.models.approval import ApprovalStatus
from app.models.content import (
    ContentChannel,
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication import Publication, PublicationStatus
from app.models.user import User
from app.schemas.publication import PublicationCreate
from app.services.approval_service import ApprovalService
from app.services.publication_service import PublicationService
from app.tests.test_smm_integration import smm_fixture


async def _approved_post(session: AsyncSession):
    _task, campaign, article_version = await smm_fixture(session)
    user = await session.scalar(select(User))
    assert user is not None
    agent_id = _task.assigned_agent_id
    post = ContentItem(
        campaign_id=campaign.id,
        source_task_id=_task.id,
        content_type=ContentType.SOCIAL_POST,
        title="Approved post",
        status=ContentStatus.APPROVED,
        author_agent_id=agent_id,
        channel=ContentChannel.TELEGRAM,
    )
    session.add(post)
    await session.flush()
    version = ContentVersion(
        content_item_id=post.id,
        version_number=1,
        content="Post",
        structured_content={"text": "Post", "suggested_publish_order": 1},
        created_by_agent_id=agent_id,
    )
    session.add(version)
    await session.flush()
    post.current_version_id = version.id
    approval = await ApprovalService(session).create_content_approval(
        post.id,
        1,
        {
            "content_item_id": str(post.id),
            "content_version_id": str(version.id),
            "version_number": 1,
        },
        agent_id,
    )
    approval.status = ApprovalStatus.APPROVED
    approval.resolved_at = datetime.now(UTC)
    session.add(
        ContentDerivation(
            derived_content_version_id=version.id,
            source_content_version_id=article_version.id,
            source_section_key="problem",
        )
    )
    await session.commit()
    return user, campaign, post, version


@pytest.mark.integration
async def test_publication_lifecycle_and_provenance(db_session: AsyncSession) -> None:
    user, campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    assert publication.status is PublicationStatus.DRAFT
    assert publication.provenance[0].source_content_version_id != version.id
    assert publication.provenance[0].section_key == "problem"
    assert await db_session.scalar(
        select(ActivityLog).where(ActivityLog.event_type == "PUBLICATION_CREATED")
    )

    publication = await service.approve(publication.id, user)
    assert publication.status is PublicationStatus.APPROVED
    scheduled = datetime.now(UTC) + timedelta(days=1)
    publication = await service.schedule(publication.id, scheduled, user)
    assert publication.status is PublicationStatus.SCHEDULED
    assert publication.scheduled_at == scheduled
    rescheduled = scheduled + timedelta(days=1)
    publication = await service.schedule(publication.id, rescheduled, user)
    assert publication.scheduled_at == rescheduled
    publication = await service.cancel(publication.id, user)
    assert publication.status is PublicationStatus.CANCELLED
    assert publication.scheduled_at == rescheduled
    audit_rows = list(
        (
            await db_session.scalars(
                select(ActivityLog).where(ActivityLog.content_item_id == post.id)
            )
        ).all()
    )
    event_types = {row.event_type for row in audit_rows}
    assert {
        "PUBLICATION_CREATED",
        "PUBLICATION_APPROVED",
        "PUBLICATION_SCHEDULED",
        "PUBLICATION_RESCHEDULED",
        "PUBLICATION_CANCELLED",
    } <= event_types
    for row in audit_rows:
        assert row.campaign_id == campaign.id
        assert row.user_id == user.id
        assert row.metadata_.get("publication_id") == str(publication.id)
        assert "token" not in str(row.metadata_).lower()


@pytest.mark.integration
async def test_publication_rules_and_duplicate_protection(db_session: AsyncSession) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    created = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    with pytest.raises(AppError) as duplicate:
        await service.create(
            PublicationCreate(
                content_item_id=post.id, content_version_id=version.id, channel=post.channel
            ),
            user,
        )
    assert duplicate.value.code == "PUBLICATION_ALREADY_EXISTS"

    post.status = ContentStatus.WAITING_APPROVAL
    await db_session.commit()
    with pytest.raises(AppError) as not_approved:
        await service.create(
            PublicationCreate(
                content_item_id=post.id, content_version_id=version.id, channel=post.channel
            ),
            user,
        )
    assert not_approved.value.code == "PUBLICATION_CONTENT_NOT_APPROVED"

    post.status = ContentStatus.APPROVED
    with pytest.raises(AppError) as wrong_channel:
        await service.create(
            PublicationCreate(
                content_item_id=post.id, content_version_id=version.id, channel=ContentChannel.VK
            ),
            user,
        )
    assert wrong_channel.value.code == "PUBLICATION_CHANNEL_MISMATCH"
    with pytest.raises(AppError) as wrong_version:
        await service.create(
            PublicationCreate(
                content_item_id=post.id, content_version_id=uuid4(), channel=post.channel
            ),
            user,
        )
    assert wrong_version.value.code == "PUBLICATION_VERSION_MISMATCH"
    article = await db_session.scalar(
        select(ContentItem).where(ContentItem.content_type == ContentType.ARTICLE)
    )
    assert article is not None
    article_version = article.current_version_id
    assert article_version is not None
    with pytest.raises(AppError) as wrong_type:
        await service.create(
            PublicationCreate(
                content_item_id=article.id,
                content_version_id=article_version,
                channel=ContentChannel.TELEGRAM,
            ),
            user,
        )
    assert wrong_type.value.code == "PUBLICATION_CONTENT_TYPE_INVALID"
    article.content_type = ContentType.SOCIAL_POST_PACK
    await db_session.commit()
    with pytest.raises(AppError) as wrong_pack:
        await service.create(
            PublicationCreate(
                content_item_id=article.id,
                content_version_id=article_version,
                channel=ContentChannel.TELEGRAM,
            ),
            user,
        )
    assert wrong_pack.value.code == "PUBLICATION_CONTENT_TYPE_INVALID"

    post.status = ContentStatus.APPROVED
    created_row = await db_session.get(Publication, created.id)
    assert created_row is not None
    created_row.status = PublicationStatus.PUBLISHED
    await db_session.commit()
    with pytest.raises(AppError) as published:
        await service.cancel(created.id, user)
    assert published.value.code == "PUBLICATION_INVALID_TRANSITION"


@pytest.mark.integration
async def test_publication_creation_is_serialized_for_competing_sessions(
    db_session: AsyncSession,
) -> None:
    import asyncio

    from app.core.database import async_session_factory

    user, _campaign, post, version = await _approved_post(db_session)

    async def attempt():
        async with async_session_factory() as session:
            return await PublicationService(session).create(
                PublicationCreate(
                    content_item_id=post.id,
                    content_version_id=version.id,
                    channel=post.channel,
                ),
                user,
            )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, AppError)]
    assert len(successes) == 1
    assert len(failures) == 1


@pytest.mark.integration
async def test_publication_requires_timezone_and_future_schedule(db_session: AsyncSession) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(publication.id, user)
    with pytest.raises(AppError) as naive:
        await service.schedule(publication.id, datetime.now(), user)
    assert naive.value.code == "PUBLICATION_TIMEZONE_REQUIRED"
    with pytest.raises(AppError) as past:
        await service.schedule(publication.id, datetime.now(UTC) - timedelta(minutes=1), user)
    assert past.value.code == "PUBLICATION_TIME_IN_PAST"


@dataclass
class _FakeTelegramProvider:
    calls: int = 0
    failure: TelegramProviderError | None = None

    async def publish(self, *, text: str, chat_id: str) -> ProviderPublicationResult:
        self.calls += 1
        assert text == "Post"
        assert isinstance(chat_id, str)
        if self.failure:
            raise self.failure
        return ProviderPublicationResult("telegram-1", "https://t.me/c/1", datetime.now(UTC))


@pytest.mark.integration
async def test_telegram_publish_uses_exact_version_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(publication.id, user)
    claimed = await service.claim_for_publish(publication.id, user)
    assert claimed.status is PublicationStatus.PUBLISHING
    provider = _FakeTelegramProvider()
    result = await service.execute_telegram(publication.id, provider)
    assert result is not None and result.status is PublicationStatus.PUBLISHED
    assert result.external_id == "telegram-1"
    assert provider.calls == 1
    assert await service.execute_telegram(publication.id, provider) is None
    assert provider.calls == 1


@pytest.mark.integration
async def test_telegram_publish_rejects_vk_and_draft(db_session: AsyncSession) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    post.channel = ContentChannel.VK
    await db_session.commit()
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    with pytest.raises(AppError) as draft:
        await service.claim_for_publish(publication.id, user)
    assert draft.value.code == "PUBLICATION_INVALID_STATE"
    await service.approve(publication.id, user)
    with pytest.raises(AppError) as unsupported:
        await service.claim_for_publish(publication.id, user)
    assert unsupported.value.code == "PUBLICATION_CHANNEL_UNSUPPORTED"


@pytest.mark.integration
async def test_telegram_ambiguous_failure_requires_reconciliation(db_session: AsyncSession) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(publication.id, user)
    await service.claim_for_publish(publication.id, user)
    result = await service.execute_telegram(
        publication.id,
        _FakeTelegramProvider(
            failure=TelegramProviderError("TELEGRAM_PROVIDER_TIMEOUT", "timeout", ambiguous=True)
        ),
    )
    assert result is not None
    assert result.status is PublicationStatus.FAILED
    assert result.failure_code == "TELEGRAM_RECONCILIATION_REQUIRED"


@pytest.mark.integration
async def test_stuck_publishing_recovery_is_conservative(db_session: AsyncSession) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(publication.id, user)
    await service.claim_for_publish(publication.id, user)
    row = await db_session.get(Publication, publication.id)
    assert row is not None
    row.updated_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    recovered_count = await service.recover_stuck_publishing(
        cutoff=datetime.now(UTC) - timedelta(minutes=5)
    )
    assert recovered_count == 1
    recovered = await db_session.get(Publication, publication.id)
    assert recovered is not None
    assert recovered.status is PublicationStatus.FAILED
    assert recovered.failure_code == "TELEGRAM_RECONCILIATION_REQUIRED"


@pytest.mark.integration
async def test_concurrent_claims_have_one_owner_and_one_send(db_session: AsyncSession) -> None:
    import asyncio

    from app.core.database import async_session_factory

    user, _campaign, post, version = await _approved_post(db_session)
    created = await PublicationService(db_session).create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await PublicationService(db_session).approve(created.id, user)

    async def claim():
        async with async_session_factory() as session:
            try:
                return await PublicationService(session).claim_for_publish(created.id, user)
            except Exception as error:
                return error

    results = await asyncio.gather(claim(), claim())
    owners = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, AppError)]
    assert len(owners) == 1
    assert len(failures) == 1
    provider = _FakeTelegramProvider()
    async with async_session_factory() as session:
        await PublicationService(session).execute_telegram(created.id, provider)
    assert provider.calls == 1


@pytest.mark.integration
async def test_scheduled_dispatch_only_claims_due_publications(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workers import dispatcher_worker

    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    due = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(due.id, user)
    await service.schedule(due.id, datetime.now(UTC) + timedelta(minutes=5), user)
    calls: list[str] = []
    monkeypatch.setattr(dispatcher_worker.publish_telegram_publication, "delay", calls.append)
    await dispatcher_worker._dispatch_publications()
    assert calls == []
    row = await db_session.get(Publication, due.id)
    assert row is not None and row.status is PublicationStatus.SCHEDULED
    row.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    await dispatcher_worker._dispatch_publications()
    assert calls == [str(due.id)]
    row = await db_session.get(Publication, due.id)
    assert row is not None
    await db_session.refresh(row)
    assert row.status is PublicationStatus.PUBLISHING
    await dispatcher_worker._dispatch_publications()
    assert calls == [str(due.id)]


@pytest.mark.integration
async def test_publication_keeps_bound_approved_version_when_newer_version_is_current(
    db_session: AsyncSession,
) -> None:
    user, _campaign, post, version = await _approved_post(db_session)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id, content_version_id=version.id, channel=post.channel
        ),
        user,
    )
    await service.approve(publication.id, user)
    await service.claim_for_publish(publication.id, user)
    newer = ContentVersion(
        content_item_id=post.id,
        version_number=2,
        content="Newer content",
        structured_content={"text": "Newer content"},
    )
    db_session.add(newer)
    await db_session.flush()
    post.current_version_id = newer.id
    await db_session.commit()
    provider = _FakeTelegramProvider()
    result = await service.execute_telegram(publication.id, provider)
    assert result is not None and result.status is PublicationStatus.PUBLISHED
    assert provider.calls == 1


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [
        (401, "TELEGRAM_AUTH_ERROR", False),
        (403, "TELEGRAM_PERMISSION_ERROR", False),
        (400, "TELEGRAM_BAD_REQUEST", False),
        (429, "TELEGRAM_RATE_LIMIT", True),
    ],
)
async def test_telegram_provider_failure_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: str,
    retryable: bool,
) -> None:
    class _Response:
        def __init__(self, code: int):
            self.status_code = code

        def json(self):
            return {"ok": False}

    class _Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 30

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return _Response(status_code)

    monkeypatch.setattr("app.integrations.telegram.httpx.AsyncClient", _Client)
    with pytest.raises(TelegramProviderError) as error:
        await TelegramProvider("fake-bot-token").publish(text="Привет", chat_id="-100")
    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    assert "fake-bot-token" not in error.value.safe_message


async def test_telegram_provider_timeout_is_ambiguous_and_network_connect_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("app.integrations.telegram.httpx.AsyncClient", _Client)
    with pytest.raises(TelegramProviderError) as error:
        await TelegramProvider("fake-bot-token").publish(text="Привет", chat_id="-100")
    assert error.value.code == "TELEGRAM_PROVIDER_ERROR"
    assert error.value.retryable is True
    assert error.value.ambiguous is False


@pytest.mark.integration
async def test_publication_api_returns_bound_provenance(client, db_session: AsyncSession) -> None:
    user, campaign, post, version = await _approved_post(db_session)

    async def current_user():
        return user

    app.dependency_overrides[get_current_user] = current_user
    response = await client.post(
        "/api/v1/publications",
        json={
            "content_item_id": str(post.id),
            "content_version_id": str(version.id),
            "channel": post.channel.value,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["campaign_id"] == str(campaign.id)
    assert body["content_version_id"] == str(version.id)
    assert body["provenance"][0]["source_content_version_id"] != str(version.id)
    listed = await client.get(f"/api/v1/publications/campaign/{campaign.id}")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    cross_campaign = await client.post(
        "/api/v1/publications",
        json={
            "campaign_id": str(uuid4()),
            "content_item_id": str(post.id),
            "content_version_id": str(version.id),
            "channel": post.channel.value,
        },
    )
    assert cross_campaign.status_code == 422
