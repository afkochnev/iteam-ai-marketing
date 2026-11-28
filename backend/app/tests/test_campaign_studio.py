import json
import os
from base64 import b64encode
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from httpx import (
    URL,
    ASGITransport,
    AsyncClient,
    AsyncHTTPTransport,
    MockTransport,
    ReadTimeout,
    Request,
    Response,
)
from PIL import Image
from sqlalchemy import Connection, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.api.dependencies import get_read_snapshot_session
from app.core.config import settings
from app.core.database import get_db_session
from app.core.security import encode_access_token
from app.integrations.studio_wordpress import (
    WordPressArticle,
    WordPressError,
    WordPressImage,
    WordPressStudioClient,
    WordPressTarget,
)
from app.main import app
from app.models.campaign import Campaign
from app.models.creative_generation import CreativeGenerationStatus
from app.models.studio_wordpress import StudioWordPressDelivery, StudioWordPressOperation
from app.models.user import User, UserRole
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMembership, WorkspaceRole
from app.services.creative_generation_service import CreativeGenerationService
from app.workers import creative_generation_tasks, studio_wordpress_worker
from app.workers.studio_wordpress_worker import run_next_wordpress_operation

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


def sample_png(color: str = "green") -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (26, 20), color) as image:
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
            "caption": "A picture <green>",
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
        "title": "Guide <2026>",
        "body": "Revised draft\nNext & final",
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
    approved_visual = await client.post(f"{path}/versions/{visual_id}/approve")
    assert approved_visual.status_code == 200, approved_visual.text
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

    historical_version = await client.get(f"{path}/versions/{visual_id}")
    assert historical_version.status_code == 200, historical_version.text
    assert historical_version.headers["cache-control"] == "private, no-store"
    assert historical_version.json() == approved_visual.json()
    assert (await client.get(f"{other_path}/versions/{visual_id}")).status_code == 404

    package_path = f"{path}/versions/{article_v2['id']}/website-package"
    next_article = await client.post(
        revision_path,
        json={
            **request,
            "body": "New draft",
            "expected_current_version_id": article_v2["id"],
            "illustration_version_id": visual_v2["id"],
        },
    )
    assert next_article.status_code == 201, next_article.text
    assert (
        await client.get(f"{path}/versions/{next_article.json()['id']}/website-package")
    ).status_code == 409
    assert (await client.get(f"{path}/versions/{visual_id}/website-package")).status_code == 422
    assert (
        await client.get(f"{other_path}/versions/{article_v2['id']}/website-package")
    ).status_code == 404

    package = await client.get(package_path)
    assert package.status_code == 200, package.text
    assert package.headers["content-type"] == "application/zip"
    assert package.headers["cache-control"] == "private, no-store"
    assert package.headers["content-disposition"] == (
        f'attachment; filename="article-{article_v2["id"]}.zip"'
    )
    with ZipFile(BytesIO(package.content)) as archive:
        assert set(archive.namelist()) == {"article.html", "manifest.json", "illustration.webp"}
        assert archive.read("illustration.webp") == variant.content
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["campaign_id"] == str(campaign.id)
        assert manifest["article"]["version_id"] == article_v2["id"]
        assert manifest["article"]["snapshot_hash"] == article_v2["snapshot_hash"]
        assert manifest["illustration"]["version_id"] == visual_id
        assert manifest["illustration"]["snapshot_hash"] == visual_v1["snapshot_hash"]
        assert manifest["illustration"]["usage_rights"] == visual_v1["payload"]["usage_rights"]
        assert "brief" not in manifest["illustration"]
        html = archive.read("article.html").decode("utf-8")
        assert "<h1>Guide &lt;2026&gt;</h1>" in html
        assert "Revised draft\nNext &amp; final" in html
        assert 'src="illustration.webp" alt="A green card"' in html
        assert "<figcaption>A picture &lt;green&gt;</figcaption>" in html
        assert "New draft" not in html
    assert (await client.get(package_path)).content == package.content

    digest = manifest["illustration"]["media"]["sha256"]
    (tmp_path / digest[:2] / digest).write_bytes(b"corrupt image")
    unavailable = await client.get(package_path)
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "CREATIVE_MEDIA_UNAVAILABLE"

    authenticate(client, outsider)
    assert (await client.get(f"{path}/versions/{visual_id}")).status_code == 403
    assert (await client.get(package_path)).status_code == 403


async def test_website_package_for_approved_article_without_illustration(
    studio_client: AsyncClient, db_session: AsyncSession
) -> None:
    editor, _, campaign, _ = await seed_studio(db_session)
    authenticate(studio_client, editor)
    path = f"/api/v1/campaigns/{campaign.id}/studio"
    draft = await studio_client.post(
        f"{path}/assets/articles", json={"title": "План кампании", "body": "Текст статьи"}
    )
    assert draft.status_code == 201, draft.text
    version = f"{path}/versions/{draft.json()['id']}"
    package_path = f"{version}/website-package"
    rejected = await studio_client.get(package_path)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "STUDIO_ARTICLE_APPROVAL_REQUIRED"

    assert (await studio_client.post(f"{version}/approve")).status_code == 200
    package = await studio_client.get(package_path)
    assert package.status_code == 200, package.text
    with ZipFile(BytesIO(package.content)) as archive:
        assert set(archive.namelist()) == {"article.html", "manifest.json"}
        assert "<h1>План кампании</h1>" in archive.read("article.html").decode("utf-8")
        assert json.loads(archive.read("manifest.json"))["illustration"] is None


async def test_sandbox_draft_and_publication_pin_exact_approved_materials(
    studio_client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "creative_media_root", tmp_path)
    editor, outsider, campaign, other_campaign = await seed_studio(db_session)
    client = studio_client
    authenticate(client, editor)
    path = f"/api/v1/campaigns/{campaign.id}/studio"
    visual = await client.post(
        f"{path}/assets/illustrations",
        data={
            "title": "Diagram",
            "brief": "Draw a diagram",
            "brand_profile": "Green and white",
            "alt_text": "Figure <one>",
            "caption": "Example",
            "usage_rights": "Original work",
        },
        files={"file": ("cover.png", sample_png(), "image/png")},
    )
    assert visual.status_code == 201, visual.text
    visual_id = visual.json()["id"]
    assert (await client.post(f"{path}/versions/{visual_id}/approve")).status_code == 200
    draft = await client.post(
        f"{path}/assets/articles",
        json={
            "title": "Title <original>",
            "body": "First & final",
            "illustration_version_id": visual_id,
        },
    )
    assert draft.status_code == 201, draft.text
    article = draft.json()
    version_path = f"{path}/versions/{article['id']}"
    sandbox = f"{version_path}/website-sandbox"
    metadata = {"seo_title": "Search title", "seo_description": 'With "quotes"'}
    assert (await client.get(sandbox)).json() == {"enabled": False, "page": None}
    assert (await client.post(f"{sandbox}/draft", json=metadata)).status_code == 404
    monkeypatch.setattr(settings, "studio_website_sandbox_enabled", True)
    assert (await client.post(f"{sandbox}/draft", json=metadata)).status_code == 409
    assert (await client.post(f"{version_path}/approve")).status_code == 200
    assert (await client.get(sandbox)).json() == {"enabled": True, "page": None}

    response = await client.post(f"{sandbox}/draft", json=metadata)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["article_version_id"] == article["id"]
    assert page["illustration_version_id"] == visual_id
    assert page["published_at"] is None
    assert page["metadata"] == metadata
    assert (await client.post(f"{sandbox}/draft", json=metadata)).json() == page
    different = await client.post(f"{sandbox}/draft", json={"seo_title": "New SEO title"})
    assert different.status_code == 409
    status = await client.get(sandbox)
    assert status.headers["cache-control"] == "private, no-store"
    assert status.json() == {"enabled": True, "page": page}
    page_id = page["id"]
    public_url = f"/api/v1/website-sandbox/{page_id}"
    preview_url = f"{sandbox}/{page_id}/preview"
    publish_url = f"{sandbox}/{page_id}/publish"
    assert (await client.get(public_url)).status_code == 404
    assert (await client.post(f"{sandbox}/{uuid4()}/publish")).status_code == 404

    preview = await client.get(preview_url)
    assert preview.status_code == 200, preview.text
    assert preview.headers["x-robots-tag"] == "noindex, nofollow"
    assert "<title>Search title</title>" in preview.text
    assert 'content="With &quot;quotes&quot;"' in preview.text
    assert "<h1>Title &lt;original&gt;</h1>" in preview.text
    assert "First &amp; final" in preview.text
    assert 'alt="Figure &lt;one&gt;"' in preview.text
    media = await client.get(f"{path}/versions/{visual_id}/media/website")
    assert media.status_code == 200
    assert b64encode(media.content).decode("ascii") in preview.text

    other_path = sandbox.replace(str(campaign.id), str(other_campaign.id))
    assert (await client.post(f"{other_path}/{page_id}/publish")).status_code == 404
    authenticate(client, outsider)
    assert (await client.get(sandbox)).status_code == 403
    assert (await client.get(preview_url)).status_code == 403
    assert (await client.post(publish_url)).status_code == 403
    authenticate(client, editor)

    new_draft = await client.post(
        f"{path}/assets/articles/{article['asset_id']}/versions",
        json={
            "title": "Later title",
            "body": "Later draft",
            "expected_current_version_id": article["id"],
        },
    )
    assert new_draft.status_code == 201, new_draft.text

    published = await client.post(publish_url)
    assert published.status_code == 200, published.text
    assert published.json()["published_at"] is not None
    assert published.json()["metadata"] == metadata
    assert (await client.post(publish_url)).json() == published.json()
    assert (await client.get(sandbox)).json() == {"enabled": True, "page": published.json()}
    client.cookies.clear()
    assert (await client.get(preview_url)).status_code == 401
    public = await client.get(public_url)
    assert public.status_code == 200, public.text
    assert public.text == preview.text
    assert "Later draft" not in public.text
    monkeypatch.setattr(settings, "app_env", "production")
    assert (await client.get(public_url)).status_code == 404


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


async def test_wordpress_reads_the_full_approved_article_in_a_bounded_response() -> None:
    article = WordPressArticle("iteam-fixture", "Guide", "<p>" + "\U0001f600" * 100_000 + "</p>")

    def wordpress(request: Request) -> Response:
        assert request.method == "GET"
        assert request.url.path == "/wp-json/wp/v2/posts"
        assert request.url.params["per_page"] == "2"
        assert set(request.url.params["_fields"].split(",")) == {
            "id",
            "type",
            "status",
            "slug",
            "author",
            "title.raw",
            "content.raw",
            "featured_media",
            "link",
        }
        payload = json.dumps(
            [
                {
                    "id": 41,
                    "type": "post",
                    "status": "draft",
                    "slug": article.slug,
                    "author": 7,
                    "title": {"raw": article.title},
                    "content": {"raw": article.html},
                    "featured_media": 0,
                }
            ],
            ensure_ascii=True,
        )
        assert len(payload) > 1_000_000
        return Response(200, content=payload)

    async with AsyncClient(transport=MockTransport(wordpress)) as remote:
        target = WordPressTarget("https://wordpress.example.test", 7)
        adapter = WordPressStudioClient(remote, target, "editor", "test")
        draft = await adapter.find_draft(article)
        assert draft is not None and draft.id == 41
        assert not adapter.write_attempted


@pytest.mark.parametrize("endpoint", ["posts", "media"])
@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ([], None),
        ([{}], "WORDPRESS_RESPONSE_MISMATCH"),
        ({}, "WORDPRESS_RESPONSE_MISMATCH"),
        ([{}, {}], "WORDPRESS_LOOKUP_AMBIGUOUS"),
    ],
)
async def test_wordpress_lookup_distinguishes_no_match_from_invalid_or_ambiguous_response(
    endpoint: str, payload: object, error_code: str | None
) -> None:
    def wordpress(request: Request) -> Response:
        assert request.method == "GET"
        assert request.url.path == f"/wp-json/wp/v2/{endpoint}"
        return Response(200, json=payload)

    async with AsyncClient(transport=MockTransport(wordpress)) as remote:
        target = WordPressTarget("https://wordpress.example.test", 1)
        adapter = WordPressStudioClient(remote, target, "editor", "test")
        lookup = (
            adapter.find_draft(WordPressArticle("fixture", "Guide", "<p>Text</p>"))
            if endpoint == "posts"
            else adapter.find_image(WordPressImage("fixture-image", "0" * 64, 1, "Cover"), 1)
        )
        if error_code is None:
            assert await lookup is None
        else:
            with pytest.raises(WordPressError, match=error_code):
                await lookup
        assert not adapter.write_attempted


@pytest.mark.parametrize(
    ("field", "malformed"),
    [("author", True), ("author", 1.0), ("featured_media", False), ("featured_media", 0.0)],
)
async def test_wordpress_draft_lookups_require_integer_author_and_featured_media_ids(
    field: str, malformed: bool | float
) -> None:
    article = WordPressArticle("fixture", "Guide", "<p>Text</p>")
    candidate: dict[str, Any] = {
        "id": 41,
        "type": "post",
        "status": "draft",
        "slug": article.slug,
        "author": 1,
        "title": {"raw": article.title},
        "content": {"raw": article.html},
        "featured_media": 0,
    }

    def wordpress(request: Request) -> Response:
        assert request.method == "GET"
        assert request.url.path == "/wp-json/wp/v2/posts"
        return Response(200, json=[candidate])

    async with AsyncClient(transport=MockTransport(wordpress)) as remote:
        target = WordPressTarget("https://wordpress.example.test", 1)
        adapter = WordPressStudioClient(remote, target, "editor", "test")
        accepted = await adapter.find_draft(article)
        assert accepted is not None and accepted.id == 41
        candidate[field] = malformed
        with pytest.raises(WordPressError, match="WORDPRESS_RESPONSE_MISMATCH"):
            await adapter.find_draft(article)
        assert not adapter.write_attempted


@pytest.mark.parametrize(
    ("field", "malformed"), [("author", True), ("author", 1.0), ("post", True), ("post", 1.0)]
)
async def test_wordpress_image_lookups_require_integer_author_and_parent_post_ids(
    field: str, malformed: bool | float
) -> None:
    content = b"illustration fixture"
    image = WordPressImage("fixture-image", sha256(content).hexdigest(), len(content), "Cover")
    target = WordPressTarget("https://wordpress.example.test", 1)
    image_path = "/wp-content/uploads/cover.webp"
    candidate: dict[str, Any] = {
        "id": 42,
        "type": "attachment",
        "slug": image.slug,
        "mime_type": "image/webp",
        "author": 1,
        "post": 1,
        "alt_text": image.alt,
        "source_url": target.url + image_path,
    }
    downloads = []

    def wordpress(request: Request) -> Response:
        assert request.method == "GET"
        if request.url.path == image_path:
            downloads.append(request.url.path)
            return Response(200, content=content)
        assert request.url.path == "/wp-json/wp/v2/media"
        return Response(200, json=[candidate])

    async with AsyncClient(transport=MockTransport(wordpress)) as remote:
        adapter = WordPressStudioClient(remote, target, "editor", "test")
        assert await adapter.find_image(image, 1) == 42
        candidate[field] = malformed
        with pytest.raises(WordPressError, match="WORDPRESS_RESPONSE_MISMATCH"):
            await adapter.find_image(image, 1)
        assert downloads == [image_path]
        assert not adapter.write_attempted


class FrozenWordPressClock(datetime):
    current: datetime

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        assert tz is UTC
        return cls.current


async def create_wordpress_article(client: AsyncClient, path: str) -> tuple[str, bytes]:
    visual = await client.post(
        f"{path}/assets/illustrations",
        data={
            "title": "Cover",
            "brief": "One green card",
            "brand_profile": "Green and white",
            "alt_text": "A card",
            "usage_rights": "Original editorial artwork",
        },
        files={"file": ("cover.png", sample_png(), "image/png")},
    )
    assert visual.status_code == 201, visual.text
    image_version = visual.json()["id"]
    assert (await client.post(f"{path}/versions/{image_version}/approve")).status_code == 200
    image_response = await client.get(f"{path}/versions/{image_version}/media/website")
    assert image_response.status_code == 200, image_response.text
    image_bytes = image_response.content
    article = await client.post(
        f"{path}/assets/articles",
        json={"title": "Guide", "body": "A < B\nNext", "illustration_version_id": image_version},
    )
    assert article.status_code == 201, article.text
    return article.json()["id"], image_bytes


async def test_wordpress_0039_upgrade_preserves_existing_publication(
    studio_client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "creative_media_root", tmp_path)

    source = "20261126_0039"
    schedule = "20261128_0040"
    target = "20261128_0041"
    # Exercise exact migrations on the fixture connection. Its rollback contains
    # both the PostgreSQL DDL and the records, even if an assertion fails.
    assert await db_session.scalar(text("SELECT version_num FROM alembic_version")) == target
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    schedule_script = scripts.get_revision(schedule)
    target_script = scripts.get_revision(target)
    assert schedule_script is not None and schedule_script.down_revision == source
    assert target_script is not None and target_script.down_revision == schedule
    connection = await db_session.connection()

    async def migrate(*, upgrade: bool) -> None:
        def run_migrations(sync_connection: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_connection)):
                if upgrade:
                    schedule_script.module.upgrade()
                    target_script.module.upgrade()
                else:
                    target_script.module.downgrade()
                    schedule_script.module.downgrade()

        await connection.run_sync(run_migrations)

    async def original_rows() -> dict[str, list[str]]:
        projections = {
            "campaigns": "to_jsonb(r)",
            "content_assets": "to_jsonb(r)",
            "content_asset_versions": "to_jsonb(r)",
            "content_asset_approvals": "to_jsonb(r)",
            "studio_wordpress_deliveries": "to_jsonb(r)",
            "studio_wordpress_operations": "to_jsonb(r) - 'scheduled_for'",
        }
        return {
            table: list(
                await db_session.scalars(
                    text(f"SELECT ({projection})::text FROM {table} r ORDER BY 1")
                )
            )
            for table, projection in projections.items()
        }

    async def has_schedule_column() -> bool:
        return bool(
            await db_session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'studio_wordpress_operations' "
                    "AND column_name = 'scheduled_for')"
                )
            )
        )

    await migrate(upgrade=False)
    assert not await has_schedule_column()
    assert await db_session.scalar(text("SELECT to_regclass('studio_wordpress_revisions')")) is None

    editor, _, campaign, _ = await seed_studio(db_session)
    authenticate(studio_client, editor)
    studio_path = f"/api/v1/campaigns/{campaign.id}/studio"
    version, _ = await create_wordpress_article(studio_client, studio_path)
    article = await studio_client.post(f"{studio_path}/versions/{version}/approve")
    assert article.status_code == 200, article.text
    site = "https://wordpress.example.test/staging"
    canonical = f"{site}/guide/"
    delivery = StudioWordPressDelivery(
        workspace_id=DEFAULT_WORKSPACE_ID,
        campaign_id=campaign.id,
        article_version_id=UUID(version),
        illustration_version_id=UUID(article.json()["illustration_version_id"]),
        target_url=site,
        target_author_id=42,
    )
    db_session.add(delivery)
    await db_session.flush()

    now = datetime.now(UTC)
    for action, remote_id, remote_url in (
        ("draft", 41, None),
        ("image", 31, None),
        ("publish", 41, canonical),
    ):
        operation_id = uuid4()
        await db_session.execute(
            text(
                "INSERT INTO studio_wordpress_operations "
                "(id, delivery_id, action, status, requested_by) "
                "VALUES (:id, :delivery_id, :action, 'queued', :requested_by)"
            ),
            {
                "id": operation_id,
                "delivery_id": delivery.id,
                "action": action,
                "requested_by": editor.id,
            },
        )
        await db_session.execute(
            text(
                "UPDATE studio_wordpress_operations SET status = 'running', started_at = :now "
                "WHERE id = :id"
            ),
            {"id": operation_id, "now": now},
        )
        await db_session.execute(
            text(
                "UPDATE studio_wordpress_operations SET status = 'succeeded', "
                "finished_at = :now, remote_id = :remote_id, remote_url = :remote_url "
                "WHERE id = :id"
            ),
            {"id": operation_id, "now": now, "remote_id": remote_id, "remote_url": remote_url},
        )

    expected = await original_rows()
    assert len(expected["content_asset_versions"]) == len(expected["content_asset_approvals"]) == 2
    assert len(expected["studio_wordpress_operations"]) == 3

    await migrate(upgrade=True)
    assert await has_schedule_column()
    assert await original_rows() == expected
    assert list(
        await db_session.scalars(
            text("SELECT scheduled_for FROM studio_wordpress_operations ORDER BY action")
        )
    ) == [None, None, None]
    assert await db_session.scalar(text("SELECT count(*) FROM studio_wordpress_revisions")) == 0

    placement = await studio_client.get(f"/api/v1/campaigns/{campaign.id}/performance/wordpress")
    assert placement.status_code == 200, placement.text
    (page,) = placement.json()["placements"]
    assert (page["placement_id"], page["post_id"], page["canonical_url"]) == (
        str(delivery.id),
        "41",
        canonical,
    )
    assert page["current_version_id"] == version
    assert page["current_revision_id"] is None
    assert page["current_version_number"] == 1

    await migrate(upgrade=False)
    assert not await has_schedule_column()
    assert await original_rows() == expected
    await migrate(upgrade=True)
    assert await original_rows() == expected


@pytest.mark.parametrize("schedule_publication", [False, True], ids=["immediate", "scheduled"])
async def test_staging_wordpress_reconciles_lost_responses_without_repeating_writes(
    schedule_publication: bool,
    studio_client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = "https://wordpress.example.test/staging"
    monkeypatch.setattr(settings, "studio_wordpress_url", origin)
    monkeypatch.setattr(settings, "studio_wordpress_author_id", 7)
    monkeypatch.setattr(settings, "creative_media_root", tmp_path)
    editor, outsider, campaign, _ = await seed_studio(db_session)
    authenticate(studio_client, editor)
    path = f"/api/v1/campaigns/{campaign.id}/studio"
    version, image_bytes = await create_wordpress_article(studio_client, path)
    route = f"{path}/versions/{version}/wordpress"
    confirmation = {"target_url": origin, "target_author_id": "7"}
    assert (await studio_client.post(f"{route}/draft", json=confirmation)).status_code == 409
    assert (await studio_client.post(f"{path}/versions/{version}/approve")).status_code == 200
    authenticate(studio_client, outsider)
    assert (await studio_client.post(f"{route}/draft", json=confirmation)).status_code == 403
    assert (await studio_client.get(route)).status_code == 403
    authenticate(studio_client, editor)
    assert (
        await studio_client.post(
            f"{route}/draft",
            json={
                **confirmation,
                "target_author_id": "99",
            },
        )
    ).status_code == 409

    calls: list[tuple[str, str]] = []
    post: dict[str, Any] = {}
    image: dict[str, Any] = {}
    visible_in_lookup = False
    lookup_failed = False

    def wordpress(request: Request) -> Response:
        endpoint = request.url.path.removeprefix("/staging/wp-json/wp/v2")
        calls.append((request.method, endpoint))
        assert request.url.host == "wordpress.example.test"
        if endpoint == "/staging/uploads/cover.webp":
            assert "Authorization" not in request.headers
            return Response(200, content=image_bytes)
        assert "Authorization" in request.headers
        assert request.url.params["context"] == "edit"
        match request.method, endpoint:
            case "GET", "/users/me":
                return Response(200, json={"id": 7})
            case "POST", "/posts":
                payload = json.loads(request.content)
                assert payload["status"] == "draft"
                assert payload["content"] == "<p>A &lt; B</p>\n<p>Next</p>"
                post.update(
                    id=41,
                    type="post",
                    author=7,
                    status="draft",
                    slug=payload["slug"],
                    title={"raw": payload["title"]},
                    content={"raw": payload["content"]},
                    featured_media=0,
                )
                raise ReadTimeout("fixture lost draft response", request=request)
            case "GET", "/posts":
                assert request.url.params["slug"] == post["slug"]
                assert request.url.params["status"] == "draft,publish"
                if lookup_failed:
                    return Response(503)
                return Response(200, json=[post] if visible_in_lookup else [])
            case "GET", "/posts/41":
                return Response(200, json=post)
            case "POST", "/media":
                assert image_bytes in request.content
                image.update(
                    id=55,
                    type="attachment",
                    author=7,
                    slug=f"{post['slug']}-image",
                    post=41,
                    mime_type="image/webp",
                    alt_text="A card",
                    source_url=f"{origin}/uploads/cover.webp",
                )
                raise ReadTimeout("fixture lost media response", request=request)
            case "GET", "/media":
                assert request.url.params["slug"] == image["slug"]
                assert request.url.params["status"] == "inherit"
                return Response(200, json=[image])
            case "GET", "/media/55":
                return Response(200, json=image)
            case "POST", "/posts/41":
                payload = json.loads(request.content)
                assert payload["status"] == "publish" and payload["featured_media"] == 55
                assert payload["content"] == post["content"]["raw"]
                post.update(status="publish", featured_media=55, link=f"{origin}/{post['slug']}/")
                raise ReadTimeout("fixture lost publication response", request=request)
        raise AssertionError((request.method, endpoint))

    sessions = async_sessionmaker(
        await db_session.connection(),
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )

    async def delivery() -> dict[str, Any]:
        response = await studio_client.get(route)
        assert response.status_code == 200, response.text
        return response.json()

    async def state() -> dict[str, Any]:
        return {item["action"]: item for item in (await delivery())["operations"]}

    async with AsyncClient(transport=MockTransport(wordpress)) as remote:
        adapter = WordPressStudioClient(remote, WordPressTarget(origin, 7), "editor", "test")
        assert (await studio_client.post(f"{route}/draft", json=confirmation)).status_code == 200
        assert (await studio_client.post(f"{route}/draft", json=confirmation)).status_code == 200
        assert calls == []
        assert await run_next_wordpress_operation(sessions, adapter)
        assert (await state())["draft"]["status"] == "unknown"
        assert (await delivery())["draft_preview_url"] is None
        assert not await run_next_wordpress_operation(sessions, adapter)
        assert (await studio_client.post(f"{route}/publish", json=confirmation)).status_code == 409
        reconcile = f"{route}/draft/reconcile"
        lookup_failed = True
        assert (await studio_client.post(reconcile, json=confirmation)).status_code == 200
        assert await run_next_wordpress_operation(sessions, adapter)
        failed_read = (await state())["draft"]
        assert failed_read["status"] == "unknown"
        assert failed_read["failure_code"] == "WORDPRESS_CONNECTION_ERROR"
        assert failed_read["reconciliation_failure_code"] == "WORDPRESS_HTTP_ERROR"
        assert failed_read["reconciliation_finished_at"]

        lookup_failed = False
        assert (await studio_client.post(reconcile, json=confirmation)).status_code == 200
        pending_read = (await state())["draft"]
        assert pending_read["reconciliation_pending"]
        assert pending_read["reconciliation_failure_code"] is None
        assert await run_next_wordpress_operation(sessions, adapter)
        missing_receipt = (await state())["draft"]
        assert missing_receipt["status"] == "unknown"
        assert missing_receipt["reconciliation_finished_at"]
        assert missing_receipt["reconciliation_failure_code"] is None
        visible_in_lookup = True
        assert (await studio_client.post(reconcile, json=confirmation)).status_code == 200
        assert await run_next_wordpress_operation(sessions, adapter)
        confirmed = (await state())["draft"]
        assert confirmed["remote_id"] == "41"
        assert confirmed["reconciliation_failure_code"] is None
        assert (await delivery())["draft_preview_url"] == f"{origin}/?p=41&preview=true"
        with monkeypatch.context() as changed_target:
            changed_target.setattr(settings, "studio_wordpress_author_id", 99)
            changed = await delivery()
            assert changed["target_changed"] is True
            assert changed["draft_preview_url"] is None

        publish_path = f"{route}/publish"
        publication = confirmation
        scheduled_for = datetime.now(UTC) + timedelta(days=1) if schedule_publication else None
        calls_before_publication = calls.copy()
        assert confirmed["scheduled_for"] is None
        if scheduled_for is not None:
            past = await studio_client.post(
                publish_path,
                json={
                    **confirmation,
                    "scheduled_for": (scheduled_for - timedelta(days=2)).isoformat(),
                },
            )
            assert past.status_code == 409, past.text
            assert past.json()["error"]["code"] == "WORDPRESS_SCHEDULE_IN_PAST"
            naive = await studio_client.post(
                publish_path,
                json={
                    **confirmation,
                    "scheduled_for": scheduled_for.replace(tzinfo=None).isoformat(),
                },
            )
            assert naive.status_code == 422, naive.text
            assert set(await state()) == {"draft"}
            publication = {**confirmation, "scheduled_for": scheduled_for.isoformat()}

        queued = await studio_client.post(publish_path, json=publication)
        assert queued.status_code == 200, queued.text
        expected_preview = f"{origin}/?p=41&preview=true" if scheduled_for is not None else None
        assert queued.json()["draft_preview_url"] == expected_preview
        duplicate = publication
        if scheduled_for is not None:
            same_instant = scheduled_for.astimezone(timezone(timedelta(hours=2)))
            duplicate = {**confirmation, "scheduled_for": same_instant.isoformat()}
        repeated = await studio_client.post(publish_path, json=duplicate)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["operations"] == queued.json()["operations"]

        if scheduled_for is not None:
            conflicting = await studio_client.post(
                publish_path,
                json={
                    **confirmation,
                    "scheduled_for": (scheduled_for + timedelta(hours=1)).isoformat(),
                },
            )
            assert conflicting.status_code == 409, conflicting.text
            assert conflicting.json()["error"]["code"] == "WORDPRESS_SCHEDULE_CONFLICT"
            FrozenWordPressClock.current = scheduled_for - timedelta(microseconds=1)
            operation = StudioWordPressOperation
            with pytest.raises(IntegrityError, match="ck_studio_wordpress_operations_schedule"):
                async with sessions.begin() as savepoint:
                    await savepoint.execute(
                        update(operation)
                        .where(
                            operation.action == "image",
                            operation.status == "queued",
                            operation.scheduled_for == scheduled_for,
                        )
                        .values(
                            status="running",
                            started_at=FrozenWordPressClock.current,
                            lease_id=uuid4(),
                        )
                    )
            monkeypatch.setattr(studio_wordpress_worker, "datetime", FrozenWordPressClock)
            assert not await run_next_wordpress_operation(sessions, adapter)

        waiting = await state()
        for action in ("image", "publish"):
            assert waiting[action]["status"] == "queued"
            scheduled_value = waiting[action]["scheduled_for"]
            if scheduled_for is None:
                assert scheduled_value is None
            else:
                assert datetime.fromisoformat(scheduled_value) == scheduled_for
        assert calls == calls_before_publication
        if scheduled_for is not None:
            FrozenWordPressClock.current = scheduled_for

        assert await run_next_wordpress_operation(sessions, adapter)
        blocked = await state()
        assert blocked["image"]["status"] == "unknown"
        assert blocked["publish"]["status"] == "queued"
        assert not await run_next_wordpress_operation(sessions, adapter)
        reconcile = f"{route}/image/reconcile"
        assert (await studio_client.post(reconcile, json=confirmation)).status_code == 200
        assert await run_next_wordpress_operation(sessions, adapter)
        assert (await state())["image"]["remote_id"] == "55"
        assert (await state())["publish"]["status"] == "queued"
        assert await run_next_wordpress_operation(sessions, adapter)
        assert (await state())["publish"]["status"] == "unknown"
        assert not await run_next_wordpress_operation(sessions, adapter)

        reconcile = f"{route}/publish/reconcile"
        assert (await studio_client.post(reconcile, json=confirmation)).status_code == 200
        assert await run_next_wordpress_operation(sessions, adapter)
        published = (await state())["publish"]
        assert published["status"] == "succeeded"
        assert published["remote_id"] == "41"
        assert published["remote_url"] == post["link"]
        assert not await run_next_wordpress_operation(sessions, adapter)

    assert [endpoint for method, endpoint in calls if method == "POST"] == [
        "/posts",
        "/media",
        "/posts/41",
    ]
    monkeypatch.setattr(settings, "app_env", "production")
    assert (await studio_client.get(route)).json()["target_url"] is None
    assert (await studio_client.post(f"{route}/draft", json=confirmation)).status_code == 404


async def test_wordpress_performance_keeps_site_scoped_identity_across_configuration_changes(
    studio_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "test")
    editor, _, campaign, _ = await seed_studio(db_session)
    authenticate(studio_client, editor)
    route = f"/api/v1/campaigns/{campaign.id}/studio"
    created = await studio_client.post(
        f"{route}/assets/articles", json={"title": "Shared guide", "body": "One article"}
    )
    assert created.status_code == 201, created.text
    version = created.json()["id"]
    approved = await studio_client.post(f"{route}/versions/{version}/approve")
    assert approved.status_code == 200, approved.text

    first = "https://wordpress-first.example.test/blog"
    second = "https://wordpress-second.example.test/blog"
    unverified = "https://wordpress-third.example.test/blog"
    site_replies = {first: f"{first}/guide/", second: f"{second}/guide/", unverified: f"{first}/"}
    post_id = 2**53 + 1
    now = datetime.now(UTC)
    identities: dict[str, str] = {}
    for site_url, canonical_url in site_replies.items():
        delivery = StudioWordPressDelivery(
            workspace_id=DEFAULT_WORKSPACE_ID,
            campaign_id=campaign.id,
            article_version_id=UUID(version),
            target_url=site_url,
            target_author_id=42,
        )
        db_session.add(delivery)
        await db_session.flush()
        identities[site_url] = str(delivery.id)
        receipt = StudioWordPressOperation(
            delivery_id=delivery.id, action="publish", status="queued", requested_by=editor.id
        )
        db_session.add(receipt)
        await db_session.flush()
        receipt.status, receipt.started_at = "running", now
        await db_session.flush()
        receipt.status, receipt.finished_at = "succeeded", now
        receipt.remote_id, receipt.remote_url = post_id, canonical_url
        await db_session.flush()

    monkeypatch.setattr(settings, "studio_wordpress_url", None)
    monkeypatch.setattr(settings, "studio_wordpress_author_id", None)
    metrics_path = f"/api/v1/campaigns/{campaign.id}/performance/wordpress"
    response = await studio_client.get(metrics_path)
    assert response.status_code == 200, response.text
    assert response.json()["analytics_status"] == "not_configured"
    pages = response.json()["placements"]
    assert len(pages) == 2
    by_site = {page["site_url"]: page for page in pages}
    assert set(by_site) == {first, second}
    for site, page in by_site.items():
        assert page["post_id"] == str(post_id)
        assert page["placement_id"] == identities[site]
        assert page["canonical_url"] == site_replies[site]
        assert page["current_version_id"] == version
        assert page["current_revision_id"] is None

    monkeypatch.setattr(settings, "app_env", "production")
    disabled = await studio_client.get(metrics_path)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["placements"] == []


@pytest.mark.skipif(
    not os.environ.get("ITEAM_WORDPRESS_ACCEPTANCE_URL"),
    reason="Requires an isolated WordPress fixture",
)
async def test_studio_reconciles_writes_on_a_real_wordpress_site(
    studio_client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = os.environ["ITEAM_WORDPRESS_ACCEPTANCE_URL"]
    author_id = int(os.environ["ITEAM_WORDPRESS_ACCEPTANCE_AUTHOR_ID"])
    password = os.environ["ITEAM_WORDPRESS_ACCEPTANCE_PASSWORD"]
    browser_password = os.environ["ITEAM_WORDPRESS_ACCEPTANCE_BROWSER_PASSWORD"]
    target = WordPressTarget.from_config(origin, author_id)
    assert target is not None and URL(target.url).host == "127.0.0.1"
    monkeypatch.setattr(settings, "studio_wordpress_url", target.url)
    monkeypatch.setattr(settings, "studio_wordpress_author_id", target.author_id)
    monkeypatch.setattr(settings, "creative_media_root", tmp_path)

    editor, outsider, campaign, other_campaign = await seed_studio(db_session)
    authenticate(studio_client, editor)
    path = f"/api/v1/campaigns/{campaign.id}/studio"

    async def reject_provider_request(_transport: AsyncHTTPTransport, request: Request) -> Response:
        pytest.fail(f"Reading placements contacted {request.url.path}")

    async def placements(campaign_id: UUID = campaign.id) -> list[dict[str, Any]]:
        with monkeypatch.context() as isolated:
            isolated.setattr(AsyncHTTPTransport, "handle_async_request", reject_provider_request)
            response = await studio_client.get(
                f"/api/v1/campaigns/{campaign_id}/performance/wordpress"
            )
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "private, no-store"
        view = response.json()
        assert view["analytics_status"] == "not_configured"
        return view["placements"]

    assert await placements() == []
    version, image_bytes = await create_wordpress_article(studio_client, path)
    approved = await studio_client.post(f"{path}/versions/{version}/approve")
    assert approved.status_code == 200, approved.text
    route = f"{path}/versions/{version}/wordpress"
    confirmation = {"target_url": target.url, "target_author_id": str(target.author_id)}

    sessions = async_sessionmaker(
        await db_session.connection(),
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )
    writes: list[tuple[str, int]] = []

    class LostWriteResponses(AsyncHTTPTransport):
        async def handle_async_request(self, request: Request) -> Response:
            response = await super().handle_async_request(request)
            if request.method == "POST":
                writes.append((request.url.path, response.status_code))
                if response.is_success:
                    try:
                        await response.aread()
                    finally:
                        await response.aclose()
                    raise ReadTimeout(
                        "Fixture dropped a committed WordPress response", request=request
                    )
            return response

    async def operation(action: str) -> dict[str, Any]:
        response = await studio_client.get(route)
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["operations"] if item["action"] == action)

    async def command(action: str) -> None:
        response = await studio_client.post(f"{route}/{action}", json=confirmation)
        assert response.status_code == 200, response.text

    async with (
        AsyncClient(transport=LostWriteResponses(trust_env=False)) as remote,
        AsyncClient(trust_env=False) as public,
        AsyncClient(trust_env=False) as browser,
    ):
        username = "studio-author"
        adapter = WordPressStudioClient(remote, target, username, password)
        await adapter.verify_actor()

        async def reconcile_accepted_write(action: str) -> dict[str, Any]:
            assert await run_next_wordpress_operation(sessions, adapter)
            unknown = await operation(action)
            assert unknown["status"] == "unknown", (unknown, writes)
            assert unknown["failure_code"] == "WORDPRESS_CONNECTION_ERROR", (unknown, writes)
            assert await placements() == []
            assert not await run_next_wordpress_operation(sessions, adapter)
            await command(f"{action}/reconcile")
            assert await run_next_wordpress_operation(sessions, adapter)
            verified = await operation(action)
            assert verified["status"] == "succeeded", (verified, writes)
            return verified

        await command("draft")
        await command("draft")
        post_id = (await reconcile_accepted_write("draft"))["remote_id"]
        posts_url = f"{target.url}/wp-json/wp/v2/posts"
        draft_url = f"{posts_url}/{post_id}"
        assert (await public.get(draft_url)).status_code in (401, 403, 404)
        status = await studio_client.get(route)
        assert status.status_code == 200, status.text
        preview_url = status.json()["draft_preview_url"]
        assert preview_url == f"{target.url}/?p={post_id}&preview=true"
        assert (await public.get(preview_url)).status_code in (401, 403, 404)
        login_url = f"{target.url}/wp-login.php"
        assert (await browser.get(login_url)).status_code == 200
        login = await browser.post(
            login_url,
            data={
                "log": username,
                "pwd": browser_password,
                "redirect_to": preview_url,
                "testcookie": "1",
            },
        )
        assert login.is_redirect
        preview = await browser.get(preview_url)
        assert preview.status_code == 200
        assert "A &lt; B" in preview.text and "Next" in preview.text

        scheduled_for = datetime.now(UTC) + timedelta(days=1)
        queued = await studio_client.post(
            f"{route}/publish", json={**confirmation, "scheduled_for": scheduled_for.isoformat()}
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["draft_preview_url"] == preview_url
        assert await placements() == []
        FrozenWordPressClock.current = scheduled_for - timedelta(microseconds=1)
        monkeypatch.setattr(studio_wordpress_worker, "datetime", FrozenWordPressClock)
        writes_before_due = writes.copy()
        assert not await run_next_wordpress_operation(sessions, adapter)
        assert writes == writes_before_due
        assert (await public.get(draft_url)).status_code in (401, 403, 404)

        FrozenWordPressClock.current = scheduled_for
        media_id = (await reconcile_accepted_write("image"))["remote_id"]
        published = await reconcile_accepted_write("publish")
        assert published["remote_id"] == post_id
        assert not await run_next_wordpress_operation(sessions, adapter)

        pages = await placements()
        assert len(pages) == 1
        original_placement = pages[0]
        assert UUID(original_placement["placement_id"]).version == 4
        assert datetime.fromisoformat(original_placement["published_at"]) == scheduled_for
        assert original_placement == {
            "placement_id": original_placement["placement_id"],
            "site_url": target.url,
            "post_id": post_id,
            "canonical_url": published["remote_url"],
            "article_asset_id": approved.json()["asset_id"],
            "original_version_id": version,
            "current_version_id": version,
            "current_version_number": 1,
            "current_title": "Guide",
            "current_revision_id": None,
            "published_at": original_placement["published_at"],
        }
        assert await placements(other_campaign.id) == []
        authenticate(studio_client, outsider)
        denied = await studio_client.get(f"/api/v1/campaigns/{campaign.id}/performance/wordpress")
        assert denied.status_code == 403, denied.text
        authenticate(studio_client, editor)

        article = await public.get(draft_url)
        assert article.status_code == 200, article.text
        assert article.json()["featured_media"] == int(media_id)
        assert article.json()["author"] == target.author_id
        assert article.json()["link"] == published["remote_url"]
        page = await public.get(published["remote_url"])
        assert page.status_code == 200
        assert "A &lt; B" in page.text and "Next" in page.text
        media = await public.get(f"{target.url}/wp-json/wp/v2/media/{media_id}")
        assert media.status_code == 200, media.text
        assert media.json()["alt_text"] == "A card"
        uploaded = await public.get(media.json()["source_url"])
        assert uploaded.status_code == 200
        assert uploaded.content == image_bytes

        revisions_url = f"{route}/revisions"

        async def revisions() -> dict[str, Any]:
            response = await studio_client.get(revisions_url)
            assert response.status_code == 200, response.text
            return response.json()

        async def revision_status(command_id: str) -> dict[str, Any]:
            return next(
                item for item in (await revisions())["revisions"] if item["id"] == command_id
            )

        async def reconcile_revision(
            command_id: str, phase: str, confirmed_placement: dict[str, Any]
        ) -> dict[str, Any]:
            assert await run_next_wordpress_operation(sessions, adapter)
            unknown = await revision_status(command_id)
            assert unknown["status"] == "unknown", (unknown, writes)
            assert unknown["phase"] == phase
            assert unknown["failure_code"] == "WORDPRESS_CONNECTION_ERROR"
            assert await placements() == [confirmed_placement]
            assert not await run_next_wordpress_operation(sessions, adapter)
            writes_before_reconciliation = writes.copy()
            response = await studio_client.post(
                f"{revisions_url}/{command_id}/reconcile", json=confirmation
            )
            assert response.status_code == 200, response.text
            assert await run_next_wordpress_operation(sessions, adapter)
            assert writes == writes_before_reconciliation
            return await revision_status(command_id)

        original = article.json()
        initial = await revisions()
        assert initial["current_revision_id"] is None
        assert initial["current_version_id"] == version

        revised_cover = await studio_client.post(
            f"{path}/assets/illustrations",
            data={
                "title": "Revised cover",
                "brief": "One blue card",
                "brand_profile": "Blue and white",
                "alt_text": "A blue card",
                "usage_rights": "Original editorial artwork",
            },
            files={"file": ("new-cover.png", sample_png("blue"), "image/png")},
        )
        assert revised_cover.status_code == 201, revised_cover.text
        revised_cover_id = revised_cover.json()["id"]
        cover_approval = await studio_client.post(f"{path}/versions/{revised_cover_id}/approve")
        assert cover_approval.status_code == 200, cover_approval.text

        article_asset_id = approved.json()["asset_id"]
        revised_article = await studio_client.post(
            f"{path}/assets/articles/{article_asset_id}/versions",
            json={
                "title": "Guide revised",
                "body": "New description\nRevised final",
                "illustration_version_id": revised_cover_id,
                "expected_current_version_id": version,
            },
        )
        assert revised_article.status_code == 201, revised_article.text
        revised_version = revised_article.json()["id"]
        update_id = str(uuid4())
        update = {
            **confirmation,
            "command_id": update_id,
            "expected_revision_id": None,
            "article_version_id": revised_version,
            "mode": "update",
        }
        unapproved = await studio_client.post(revisions_url, json=update)
        assert unapproved.status_code == 409, unapproved.text
        assert not await run_next_wordpress_operation(sessions, adapter)

        approval = await studio_client.post(f"{path}/versions/{revised_version}/approve")
        assert approval.status_code == 200, approval.text
        options = {item["id"]: item["mode"] for item in (await revisions())["versions"]}
        assert options == {revised_version: "update"}

        for _ in range(2):
            queued_revision = await studio_client.post(revisions_url, json=update)
            assert queued_revision.status_code == 200, queued_revision.text
        assert len((await revisions())["revisions"]) == 1
        assert await placements() == [original_placement]

        image_confirmed = await reconcile_revision(update_id, "image", original_placement)
        assert image_confirmed["status"] == "queued", image_confirmed
        assert image_confirmed["phase"] == "replace"
        before_replace = await public.get(draft_url)
        assert before_replace.status_code == 200, before_replace.text
        assert before_replace.json()["featured_media"] == int(media_id)
        assert before_replace.json()["content"] == original["content"]

        updated = await reconcile_revision(update_id, "replace", original_placement)
        assert updated["status"] == "succeeded", updated
        revised_placement = {
            **original_placement,
            "current_version_id": revised_version,
            "current_version_number": 2,
            "current_title": "Guide revised",
            "current_revision_id": update_id,
        }
        assert await placements() == [revised_placement]
        current = await revisions()
        assert current["current_version_id"] == revised_version
        assert current["current_revision_id"] == update_id
        assert {item["id"]: item["mode"] for item in current["versions"]} == {version: "restore"}
        changed = await public.get(draft_url)
        assert changed.status_code == 200, changed.text
        revised_post = changed.json()
        assert revised_post["id"] == original["id"] == int(post_id)
        assert revised_post["slug"] == original["slug"]
        assert revised_post["link"] == original["link"] == published["remote_url"]
        assert revised_post["title"]["rendered"] == "Guide revised"
        assert "New description" in revised_post["content"]["rendered"]
        assert "Revised final" in revised_post["content"]["rendered"]
        revised_media_id = revised_post["featured_media"]
        assert revised_media_id > 0 and revised_media_id != int(media_id)
        new_media = await public.get(f"{target.url}/wp-json/wp/v2/media/{revised_media_id}")
        assert new_media.status_code == 200, new_media.text
        assert new_media.json()["alt_text"] == "A blue card"

        restore_id = str(uuid4())
        restore = {
            **confirmation,
            "command_id": restore_id,
            "expected_revision_id": update_id,
            "article_version_id": version,
            "mode": "restore",
        }
        stale = await studio_client.post(
            revisions_url, json={**restore, "expected_revision_id": None}
        )
        assert stale.status_code == 409, stale.text
        assert not await run_next_wordpress_operation(sessions, adapter)
        accepted = await studio_client.post(revisions_url, json=restore)
        assert accepted.status_code == 200, accepted.text
        assert await placements() == [revised_placement]
        restored = await reconcile_revision(restore_id, "replace", revised_placement)
        assert restored["status"] == "succeeded", restored
        assert await placements() == [{**original_placement, "current_revision_id": restore_id}]
        current = await revisions()
        assert current["current_version_id"] == version
        assert current["current_revision_id"] == restore_id
        assert len(current["revisions"]) == 2
        assert not await run_next_wordpress_operation(sessions, adapter)
        restored_article = await public.get(draft_url)
        assert restored_article.status_code == 200, restored_article.text
        for field in ("id", "slug", "link", "title", "content", "featured_media"):
            assert restored_article.json()[field] == original[field]

    assert writes == [
        ("/wp-json/wp/v2/posts", 201),
        ("/wp-json/wp/v2/media", 201),
        (f"/wp-json/wp/v2/posts/{post_id}", 200),
        ("/wp-json/wp/v2/media", 201),
        (f"/wp-json/wp/v2/posts/{post_id}", 200),
        (f"/wp-json/wp/v2/posts/{post_id}", 200),
    ]
