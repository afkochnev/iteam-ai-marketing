from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_read_snapshot_session
from app.core.config import settings
from app.core.database import get_db_session
from app.core.security import encode_access_token
from app.main import app
from app.models.campaign import Campaign
from app.models.creative_generation import CreativeGenerationStatus
from app.models.user import User, UserRole
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMembership, WorkspaceRole
from app.services.creative_generation_service import CreativeGenerationService
from app.workers import creative_generation_tasks

pytestmark = pytest.mark.integration


@pytest.fixture
async def studio_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    connection = await db_session.connection()

    async def studio_request_session() -> AsyncIterator[AsyncSession]:
        # Real commits at the API boundary stay contained in the test transaction.
        async with AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as session:
            yield session

    app.dependency_overrides[get_db_session] = studio_request_session
    app.dependency_overrides[get_read_snapshot_session] = studio_request_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_read_snapshot_session, None)


async def seed_studio(session: AsyncSession) -> tuple[User, User, Campaign, Campaign]:
    editor = User(email=f"{uuid4()}@example.org", password_hash="test", role=UserRole.MANAGER)
    outsider = User(email=f"{uuid4()}@example.org", password_hash="test", role=UserRole.ADMIN)
    workspace = Workspace(id=DEFAULT_WORKSPACE_ID, name="iTeam", slug="iteam", is_default=True)
    session.add_all([editor, outsider, workspace])
    await session.flush()

    campaign = Campaign(name="Trial", goal="Explain the product", created_by=editor.id)
    other_campaign = Campaign(name="Other", goal="Different audience", created_by=editor.id)
    session.add_all(
        [
            campaign,
            other_campaign,
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=editor.id, role=WorkspaceRole.MARKETING_MANAGER
            ),
        ]
    )
    await session.flush()
    return editor, outsider, campaign, other_campaign


def authenticate(client: AsyncClient, user: User) -> None:
    client.cookies.set(settings.auth_cookie_name, encode_access_token(user.id, user.auth_version))


def sample_png() -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (26, 20), "green") as image:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


async def test_article_and_illustration_workflow(
    studio_client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "creative_media_root", tmp_path)
    editor, outsider, campaign, other_campaign = await seed_studio(db_session)
    client = studio_client
    path = f"/api/v1/campaigns/{campaign.id}/studio"
    other_path = f"/api/v1/campaigns/{other_campaign.id}/studio"

    authenticate(client, outsider)
    assert (await client.get(f"{path}/assets")).status_code == 403
    authenticate(client, editor)
    article = await client.post(f"{path}/assets/articles", json={"title": "Guide", "body": "Draft"})
    assert article.status_code == 201, article.text
    article_v1 = article.json()

    image = sample_png()
    illustration = await client.post(
        f"{path}/assets/illustrations",
        data={
            "title": "Guide cover",
            "brief": "Show one simple workflow",
            "brand_profile": "Green and white",
            "alt_text": "A green card",
            "usage_rights": "Original artwork by the editorial team",
            "source_version_id": article_v1["id"],
        },
        files={"file": ("cover.png", image, "image/png")},
    )
    assert illustration.status_code == 201, illustration.text
    visual_v1 = illustration.json()
    assert visual_v1["source_version_id"] == article_v1["id"]
    assert visual_v1["payload"]["provenance"]["source"] == "UPLOAD"
    assert visual_v1["payload"]["usage_rights"] == "Original artwork by the editorial team"
    assert visual_v1["payload"]["original"]["width"] == 26
    assert {variant["name"] for variant in visual_v1["payload"]["variants"]} == {
        "website",
        "vk",
        "email",
        "presentation",
    }
    visual_id = visual_v1["id"]

    preview = await client.get(f"{path}/versions/{visual_id}/media/original")
    assert preview.status_code == 200 and preview.content == image
    assert preview.headers["content-type"] == "image/png"
    assert preview.headers["cache-control"] == "private, no-store"
    variant = await client.get(f"{path}/versions/{visual_id}/media/website")
    assert variant.status_code == 200 and variant.headers["content-type"] == "image/webp"
    assert (
        await client.get(f"{other_path}/versions/{visual_id}/media/original")
    ).status_code == 404

    article_id = article_v1["asset_id"]
    revision_path = f"{path}/assets/articles/{article_id}/versions"
    request = {
        "title": "Guide",
        "body": "Revised draft",
        "illustration_version_id": visual_id,
        "expected_current_version_id": article_v1["id"],
    }
    revision = await client.post(revision_path, json=request)
    assert revision.status_code == 201, revision.text
    article_v2 = revision.json()
    assert article_v2["supersedes_version_id"] == article_v1["id"]
    assert article_v2["illustration_version_id"] == visual_id
    assert (await client.post(revision_path, json=request)).status_code == 409

    article_approval_path = f"{path}/versions/{article_v2['id']}/approve"
    blocked = await client.post(article_approval_path)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "ILLUSTRATION_APPROVAL_REQUIRED"
    assert (await client.post(f"{path}/versions/{visual_id}/approve")).status_code == 200
    approved = await client.post(article_approval_path)
    assert approved.status_code == 200, approved.text
    assert approved.json()["approval"]["approved_by"] == str(editor.id)
    assert approved.json()["snapshot_hash"] == article_v2["snapshot_hash"]
    assert (await client.post(article_approval_path)).json()["approval"] == approved.json()[
        "approval"
    ]

    visual_asset = visual_v1["asset_id"]
    edited = await client.post(
        f"{path}/assets/illustrations/{visual_asset}/versions",
        data={
            "title": "Guide cover",
            "brief": "Show one simple workflow",
            "brand_profile": "Green and white",
            "alt_text": "A green card with a heading",
            "usage_rights": "Original artwork by the editorial team",
            "source_version_id": article_v1["id"],
            "expected_current_version_id": visual_id,
        },
    )
    assert edited.status_code == 201, edited.text
    visual_v2 = edited.json()
    assert visual_v2["supersedes_version_id"] == visual_id
    assert visual_v2["approval"] is None
    assert visual_v2["payload"]["original"] == visual_v1["payload"]["original"]
    assert visual_v2["payload"]["provenance"] == visual_v1["payload"]["provenance"]

    versions = await client.get(f"{path}/assets/{article_id}/versions")
    assert versions.status_code == 200, versions.text
    assert [version["id"] for version in versions.json()["items"]] == [
        article_v2["id"],
        article_v1["id"],
    ]
    assert versions.json()["items"][1]["approval"] is None
    assets = await client.get(f"{path}/assets", params={"kind": "ILLUSTRATION", "limit": 1})
    assert assets.status_code == 200, assets.text
    assert assets.json()["total"] == 1
    assert assets.json()["items"][0]["latest_version"]["id"] == visual_v2["id"]


@pytest.mark.parametrize(
    "status", [CreativeGenerationStatus.FAILED, CreativeGenerationStatus.UNKNOWN]
)
async def test_only_job_owner_recovers_submitted_generation_draft_and_exact_base(
    studio_client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: CreativeGenerationStatus,
) -> None:
    monkeypatch.setattr(settings, "creative_media_root", tmp_path)
    monkeypatch.setattr(settings, "creative_image_model", "test-image-model")
    monkeypatch.setattr(creative_generation_tasks, "dispatch_creative_generation", lambda _: None)
    editor, reader, campaign, other_campaign = await seed_studio(db_session)
    client = studio_client
    path = f"/api/v1/campaigns/{campaign.id}/studio"
    authenticate(client, editor)

    article = await client.post(f"{path}/assets/articles", json={"title": "Guide", "body": "Draft"})
    assert article.status_code == 201, article.text
    input_fields = {
        "title": "Guide illustration",
        "brief": "Show a simple workflow",
        "brand_profile": "Use blue and white",
        "alt_text": "A flowchart",
        "usage_rights": "Original work",
        "caption": "Figure one",
        "source_version_id": article.json()["id"],
    }
    base = await client.post(
        f"{path}/assets/illustrations",
        data=input_fields,
        files={"file": ("cover.png", sample_png(), "image/png")},
    )
    assert base.status_code == 201, base.text
    base_id, asset_id = base.json()["id"], base.json()["asset_id"]
    approval = await client.post(f"{path}/versions/{base_id}/approve")
    assert approval.status_code == 200, approval.text

    submitted_draft = {**input_fields, "brief": "Use two connected blue shapes"}
    revision = {
        "asset_id": asset_id,
        "expected_current_version_id": base_id,
    }
    submitted = await client.post(
        f"{path}/illustrations/generate", json={**submitted_draft, **revision}
    )
    assert submitted.status_code == 202, submitted.text
    assert "recovery" not in submitted.json()
    job_id = UUID(submitted.json()["id"])
    job_url = f"{path}/illustrations/generation-jobs/{job_id}"
    pending = await client.get(job_url)
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == CreativeGenerationStatus.PENDING
    assert pending.json()["recovery"] is None

    advanced = await client.post(
        f"{path}/assets/illustrations/{asset_id}/versions",
        data={**input_fields, "expected_current_version_id": base_id},
    )
    assert advanced.status_code == 201, advanced.text
    assert advanced.json()["version_number"] == 2

    jobs = CreativeGenerationService(db_session)
    if status == CreativeGenerationStatus.FAILED:
        assert await jobs.mark_dispatch_failed(job_id) is not None
    else:
        claimed = await jobs.claim(job_id)
        assert claimed is not None and claimed.claim_token is not None
        await jobs.finish_unsuccessfully(
            job_id, claimed.claim_token, "TEST_PROVIDER_UNKNOWN", outcome_unknown=True
        )

    recovered = await client.get(job_url)
    assert recovered.status_code == 200, recovered.text
    assert recovered.headers["cache-control"] == "private, no-store"
    assert recovered.json()["status"] == status
    recovery = recovered.json()["recovery"]
    assert recovery["draft"] == submitted_draft
    assert recovery["previous"] == approval.json()
    assert recovery["previous"]["id"] != advanced.json()["id"]

    other_path = f"/api/v1/campaigns/{other_campaign.id}/studio"
    wrong_campaign = await client.get(f"{other_path}/illustrations/generation-jobs/{job_id}")
    assert wrong_campaign.status_code == 404

    authenticate(client, reader)
    assert (await client.get(job_url)).status_code == 403
    db_session.add(
        WorkspaceMembership(
            workspace_id=DEFAULT_WORKSPACE_ID,
            user_id=reader.id,
            role=WorkspaceRole.MARKETING_MANAGER,
        )
    )
    await db_session.flush()
    visible_status = await client.get(job_url)
    assert visible_status.status_code == 200, visible_status.text
    assert visible_status.json()["status"] == status
    assert visible_status.json()["recovery"] is None
