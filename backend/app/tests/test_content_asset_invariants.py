from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.campaign import Campaign
from app.models.content_asset import (
    ContentAsset,
    ContentAssetApproval,
    ContentAssetKind,
    ContentAssetVersion,
)
from app.models.user import User, UserRole
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace

pytestmark = pytest.mark.integration


@pytest.fixture
async def campaign_scope(db_session: AsyncSession) -> tuple[User, Campaign, Campaign]:
    workspace = Workspace(id=DEFAULT_WORKSPACE_ID, name="iTeam", slug="iteam", is_default=True)
    author = User(
        email="editor@example.org", full_name="Editor", password_hash="test", role=UserRole.MANAGER
    )
    db_session.add_all([workspace, author])
    await db_session.flush()
    campaign = Campaign(name="Creative Studio", goal="Create an article", created_by=author.id)
    other = Campaign(name="Other campaign", goal="Separate scope", created_by=author.id)
    db_session.add_all([campaign, other])
    await db_session.flush()
    return author, campaign, other


async def _asset(
    session: AsyncSession, author: User, campaign: Campaign, kind: ContentAssetKind
) -> ContentAsset:
    row = ContentAsset(
        workspace_id=DEFAULT_WORKSPACE_ID, campaign_id=campaign.id, kind=kind, created_by=author.id
    )
    session.add(row)
    await session.flush()
    return row


async def _version(
    session: AsyncSession,
    asset: ContentAsset,
    *,
    previous: ContentAssetVersion | None = None,
    source_version_id: UUID | None = None,
    illustration_version_id: UUID | None = None,
    payload: dict[str, object] | None = None,
) -> ContentAssetVersion:
    row = ContentAssetVersion(
        asset_id=asset.id,
        workspace_id=asset.workspace_id,
        campaign_id=asset.campaign_id,
        kind=asset.kind,
        version_number=1 if previous is None else previous.version_number + 1,
        supersedes_version_id=None if previous is None else previous.id,
        source_version_id=source_version_id,
        illustration_version_id=illustration_version_id,
        payload={"title": "First draft"} if payload is None else payload,
        created_by=asset.created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def _approve(
    session: AsyncSession, version: ContentAssetVersion, *, snapshot_hash: str | None = None
) -> ContentAssetApproval:
    row = ContentAssetApproval(
        version_id=version.id,
        workspace_id=version.workspace_id,
        campaign_id=version.campaign_id,
        snapshot_hash=version.snapshot_hash if snapshot_hash is None else snapshot_hash,
        approved_by=version.created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def test_version_lineage_scope_and_immutability(
    db_session: AsyncSession, campaign_scope: tuple[User, Campaign, Campaign]
) -> None:
    author, campaign, other = campaign_scope
    article = await _asset(db_session, author, campaign, ContentAssetKind.ARTICLE)
    source = await _asset(db_session, author, campaign, ContentAssetKind.ARTICLE)
    other_asset = await _asset(db_session, author, other, ContentAssetKind.ARTICLE)
    original = await _version(db_session, article, payload={"title": "Draft", "text": "Body"})
    own_source = await _version(db_session, source, payload={"text": "Body", "title": "Draft"})
    foreign_source = await _version(db_session, other_asset)
    assert original.snapshot_hash == own_source.snapshot_hash
    assert len(original.snapshot_hash) == 64

    derived = await _version(
        db_session,
        article,
        previous=original,
        source_version_id=own_source.id,
        payload=original.payload,
    )
    assert derived.snapshot_hash != original.snapshot_hash
    assert (derived.version_number, derived.supersedes_version_id) == (2, original.id)
    assert derived.source_version_id == own_source.id

    for previous, source_id, illustration_id in (
        (original, None, None),
        (own_source, None, None),
        (derived, foreign_source.id, None),
        (derived, None, own_source.id),
    ):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _version(
                    db_session,
                    article,
                    previous=previous,
                    source_version_id=source_id,
                    illustration_version_id=illustration_id,
                )

    for overrides in (
        {"campaign_id": other.id},
        {"workspace_id": uuid4()},
        {"kind": ContentAssetKind.ILLUSTRATION},
        {"source_version_id": uuid4()},
        {"supersedes_version_id": None},
    ):
        data = dict(
            asset_id=article.id,
            workspace_id=article.workspace_id,
            campaign_id=campaign.id,
            kind=article.kind,
            version_number=3,
            supersedes_version_id=derived.id,
            payload={"title": "Third draft"},
            created_by=author.id,
        )
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(ContentAssetVersion(**(data | overrides)))
                await db_session.flush()

    for change in (
        update(ContentAssetVersion)
        .where(ContentAssetVersion.id == original.id)
        .values(payload={"invalid": "changed"}),
        delete(ContentAssetVersion).where(ContentAssetVersion.id == derived.id),
        update(ContentAsset).where(ContentAsset.id == article.id).values(campaign_id=other.id),
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            async with db_session.begin_nested():
                await db_session.execute(change)
    assert original.payload == {"title": "Draft", "text": "Body"}


async def test_approval_of_article_requires_approval_of_its_exact_visual_version(
    db_session: AsyncSession, campaign_scope: tuple[User, Campaign, Campaign]
) -> None:
    author, campaign, other = campaign_scope
    article = await _asset(db_session, author, campaign, ContentAssetKind.ARTICLE)
    visual = await _asset(db_session, author, campaign, ContentAssetKind.ILLUSTRATION)
    foreign_visual = await _asset(db_session, author, other, ContentAssetKind.ILLUSTRATION)
    foreign_version = await _version(db_session, foreign_visual)
    visual_one = await _version(db_session, visual)
    visual_two = await _version(db_session, visual, previous=visual_one)

    for asset, predecessor, linked_version in (
        (article, None, foreign_version),
        (visual, visual_two, visual_one),
    ):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                await _version(
                    db_session,
                    asset,
                    previous=predecessor,
                    illustration_version_id=linked_version.id,
                )

    article_one = await _version(db_session, article, illustration_version_id=visual_one.id)
    article_two = await _version(
        db_session, article, previous=article_one, illustration_version_id=visual_two.id
    )
    assert article_one.snapshot_hash != article_two.snapshot_hash
    # Approval belongs to the version's identity even when two snapshots have the same hash.
    assert visual_one.snapshot_hash == visual_two.snapshot_hash
    with pytest.raises(IntegrityError, match="exact illustration version"):
        async with db_session.begin_nested():
            await _approve(db_session, article_one)

    await _approve(db_session, visual_one)
    first = await _approve(db_session, article_one)
    with pytest.raises(IntegrityError, match="exact illustration version"):
        async with db_session.begin_nested():
            await _approve(db_session, article_two)

    await _approve(db_session, visual_two)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _approve(db_session, article_two, snapshot_hash=article_one.snapshot_hash)
    second = await _approve(db_session, article_two)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _approve(db_session, article_two)
    with pytest.raises(IntegrityError, match="immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(ContentAssetApproval).where(ContentAssetApproval.version_id == visual_one.id)
            )
    assert (first.version_id, first.snapshot_hash) == (article_one.id, article_one.snapshot_hash)
    assert (second.version_id, second.snapshot_hash) == (article_two.id, article_two.snapshot_hash)


async def test_downgrade_preserves_recorded_asset(
    db_session: AsyncSession, campaign_scope: tuple[User, Campaign, Campaign]
) -> None:
    author, campaign, _ = campaign_scope
    article = await _asset(db_session, author, campaign, ContentAssetKind.ARTICLE)
    directory = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            directory.get_revision("20261124_0035").module.downgrade()

    with pytest.raises(RuntimeError, match="Refusing to drop recorded content assets"):
        await (await db_session.connection()).run_sync(downgrade)
    assert await db_session.scalar(select(ContentAsset.id)) == article.id
