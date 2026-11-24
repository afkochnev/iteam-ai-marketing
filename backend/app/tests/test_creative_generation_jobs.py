from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.integrations.creative_media import CreativeImageGenerationError
from app.models.campaign import Campaign
from app.models.creative_generation import CreativeGenerationJob, CreativeGenerationStatus
from app.models.user import User, UserRole
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMembership, WorkspaceRole
from app.schemas.content_asset import IllustrationDraft
from app.services import creative_generation_service
from app.services.creative_generation_service import CreativeGenerationService
from app.workers import creative_generation_tasks


@pytest.mark.parametrize("outcome_unknown", [False, True])
async def test_generation_delivers_once_and_records_provider_classification(
    monkeypatch: pytest.MonkeyPatch, outcome_unknown: bool
) -> None:
    job_id, token = uuid4(), uuid4()
    row = SimpleNamespace(claim_token=token, prompt="Exact prompt", model="model", size="1536x1024")
    service = SimpleNamespace(
        claim=AsyncMock(side_effect=[row, None]),
        validate_claim=AsyncMock(),
        finish_unsuccessfully=AsyncMock(),
    )
    transaction_open = False

    @asynccontextmanager
    async def begin() -> AsyncIterator[object]:
        nonlocal transaction_open
        assert not transaction_open
        transaction_open = True
        try:
            yield object()
        finally:
            transaction_open = False

    async def reject_generation(**_kwargs: object) -> None:
        assert not transaction_open
        raise CreativeImageGenerationError("TEST_PROVIDER_CODE", outcome_unknown=outcome_unknown)

    provider = AsyncMock(side_effect=reject_generation)
    monkeypatch.setattr(creative_generation_tasks, "CreativeGenerationService", lambda _: service)
    monkeypatch.setattr(creative_generation_tasks, "generate_creative_image", provider)
    factory = SimpleNamespace(begin=begin)
    await creative_generation_tasks.run_creative_generation(job_id, factory)
    await creative_generation_tasks.run_creative_generation(job_id, factory)

    provider.assert_awaited_once_with(prompt="Exact prompt", model="model", size="1536x1024")
    service.validate_claim.assert_awaited_once_with(row)
    service.finish_unsuccessfully.assert_awaited_once_with(
        job_id, token, "TEST_PROVIDER_CODE", outcome_unknown=outcome_unknown
    )


@pytest.mark.integration
async def test_generation_keeps_authorization_and_fences_closed_job(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "creative_image_model", "test-image-model")
    workspace = Workspace(id=DEFAULT_WORKSPACE_ID, name="iTeam", slug="iteam", is_default=True)
    user = User(
        email=f"{uuid4()}@example.org",
        full_name="Editor",
        password_hash="test",
        role=UserRole.ADMIN,
    )
    db_session.add_all([workspace, user])
    await db_session.flush()
    campaign = Campaign(name="Campaign", goal="Content", created_by=user.id)
    db_session.add_all(
        [
            campaign,
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.WORKSPACE_ADMIN
            ),
        ]
    )
    await db_session.flush()
    draft = IllustrationDraft(
        title="Creative",
        brief="Create a diagram",
        brand_profile="Blue",
        alt_text="A diagram",
        usage_rights="Approved for internal use",
    )
    service = CreativeGenerationService(db_session)
    job = await service.create_job(
        user.id, campaign.id, draft, prompt="Exact generation prompt", size="1536x1024"
    )
    assert job.status == CreativeGenerationStatus.PENDING
    claimed = await service.claim(job.id)
    assert claimed is not None
    assert claimed.claim_token is not None
    assert claimed.draft == draft.model_dump(mode="json", exclude={"source_version_id"})
    assert (claimed.prompt, claimed.model, claimed.size) == (
        "Exact generation prompt",
        "test-image-model",
        "1536x1024",
    )
    assert await service.claim(job.id) is None
    assert await service.mark_dispatch_failed(job.id) is None

    token = claimed.claim_token
    with pytest.raises(DBAPIError, match="authorization is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(CreativeGenerationJob)
                .where(CreativeGenerationJob.id == job.id)
                .values(prompt="Unauthorized prompt")
            )

    monkeypatch.setattr(creative_generation_service, "RUNNING_LIFETIME", timedelta(0))
    await service.recover_stale_jobs()
    await service.finish_unsuccessfully(job.id, token, "LATE_FAILURE")
    finished = await service.get_job(user.id, campaign.id, job.id)
    assert (finished.status, finished.error_code) == (
        CreativeGenerationStatus.UNKNOWN,
        "CREATIVE_GENERATION_WORKER_LOST",
    )
