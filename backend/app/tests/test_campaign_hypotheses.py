from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_read_snapshot_session
from app.core.config import settings
from app.core.database import engine
from app.core.security import encode_access_token
from app.main import app
from app.models.campaign import Campaign
from app.models.hypothesis import HypothesisVersion, MarketingHypothesis
from app.models.user import User, UserRole
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMembership, WorkspaceRole
from app.tests.test_hypothesis_versions import hypothesis_snapshot

pytestmark = pytest.mark.integration

CREATED_AT = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
async def hypotheses_client(
    client: AsyncClient, db_session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    # The hypothesis immutability triggers protect committed rows from the
    # shared fixture's cleanup. Read the fixture data before it rolls back.
    async def read_test_data() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_read_snapshot_session] = read_test_data
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_read_snapshot_session)


@pytest.fixture
async def workspace(db_session: AsyncSession) -> Workspace:
    row = Workspace(id=DEFAULT_WORKSPACE_ID, name="iTeam", slug="iteam", is_default=True)
    db_session.add(row)
    await db_session.flush()
    return row


async def _user(session: AsyncSession, role: UserRole) -> User:
    user = User(email=f"{uuid4()}@example.org", full_name="Reader", password_hash="test", role=role)
    session.add(user)
    await session.flush()
    return user


async def _campaign(session: AsyncSession, user: User) -> Campaign:
    campaign = Campaign(name="Retention trial", goal="Improve setup", created_by=user.id)
    session.add(campaign)
    await session.flush()
    return campaign


async def _hypothesis(
    session: AsyncSession,
    campaign: Campaign,
    identity: int,
    *,
    created_at: datetime = CREATED_AT,
) -> MarketingHypothesis:
    row = MarketingHypothesis(
        id=UUID(int=identity),
        workspace_id=DEFAULT_WORKSPACE_ID,
        campaign_id=campaign.id,
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row


async def _version(
    session: AsyncSession,
    root: MarketingHypothesis,
    creator: User,
    *,
    prior: HypothesisVersion | None = None,
) -> HypothesisVersion:
    row = HypothesisVersion(
        hypothesis_id=root.id,
        workspace_id=root.workspace_id,
        campaign_id=root.campaign_id,
        version_number=1 if prior is None else prior.version_number + 1,
        supersedes_version_id=None if prior is None else prior.id,
        schema_version=1,
        snapshot=hypothesis_snapshot() | {"target": "35%" if prior is None else "37%"},
        created_by=creator.id,
        source="operator",
    )
    session.add(row)
    await session.flush()
    return row


def _authenticate(client: AsyncClient, user: User) -> None:
    client.cookies.set(settings.auth_cookie_name, encode_access_token(user.id, user.auth_version))


async def test_list_and_exact_version_keep_campaign_and_snapshot_identity(
    hypotheses_client: AsyncClient, db_session: AsyncSession, workspace: Workspace
) -> None:
    owner = await _user(db_session, UserRole.ADMIN)
    reader = await _user(db_session, UserRole.MANAGER)
    db_session.add(
        WorkspaceMembership(
            workspace_id=workspace.id, user_id=reader.id, role=WorkspaceRole.MARKETING_MANAGER
        )
    )
    campaign = await _campaign(db_session, owner)
    other_campaign = await _campaign(db_session, owner)
    root = await _hypothesis(db_session, campaign, 2, created_at=CREATED_AT + timedelta(days=1))
    second_root = await _hypothesis(db_session, campaign, 3)
    empty_root = await _hypothesis(db_session, campaign, 1)
    other_root = await _hypothesis(db_session, other_campaign, 4)
    first = await _version(db_session, root, owner)
    latest = await _version(db_session, root, owner, prior=first)
    await _version(db_session, second_root, owner)
    await _version(db_session, other_root, owner)
    client = hypotheses_client
    _authenticate(client, reader)
    path = f"/api/v1/campaigns/{campaign.id}/hypotheses"

    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().upper())

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        first_page = await client.get(path, params={"limit": 1})
        first_page_queries = len(statements)
        statements.clear()
        full_page = await client.get(path, params={"limit": 3})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert first_page.status_code == full_page.status_code == 200
    assert 0 < first_page_queries == len(statements) <= 6
    assert all(statement.startswith(("SELECT", "WITH")) for statement in statements)
    result = full_page.json()
    assert (result["total"], result["limit"], result["offset"]) == (3, 3, 0)
    assert datetime.fromisoformat(result["as_of"]).tzinfo is not None
    items = result["items"]
    assert [row["id"] for row in items] == [str(root.id), str(second_root.id), str(empty_root.id)]
    assert datetime.fromisoformat(items[0]["created_at"]) == root.created_at
    assert first_page.json()["items"] == items[:1]
    assert items[2]["latest_draft_version"] is None
    second_page = await client.get(path, params={"limit": 1, "offset": 1})
    assert second_page.status_code == 200
    assert second_page.json()["items"] == items[1:2]
    empty_page = await client.get(path, params={"limit": 1, "offset": 10_001})
    assert empty_page.status_code == 200
    assert (empty_page.json()["items"], empty_page.json()["total"]) == ([], 3)

    version = items[0]["latest_draft_version"]
    assert version["id"] == str(latest.id)
    assert version["version_number"] == 2
    assert version["supersedes_version_id"] == str(first.id)
    assert version["snapshot"] == latest.snapshot
    assert version["snapshot_hash"] == latest.snapshot_hash
    exact = await client.get(f"{path}/{root.id}/versions/1")
    assert exact.status_code == 200
    exact_version = exact.json()
    assert exact_version["id"] == str(first.id)
    assert datetime.fromisoformat(exact_version["created_at"]) == first.created_at
    assert exact_version["hypothesis_id"] == str(root.id)
    assert exact_version["version_number"] == 1
    assert exact_version["supersedes_version_id"] is None
    assert exact_version["schema_version"] == 1
    assert exact_version["snapshot"] == first.snapshot
    assert exact_version["snapshot_hash"] == first.snapshot_hash
    assert exact_version["created_by"] == str(owner.id)
    assert exact_version["source"] == "operator"
    for identity, number in ((root.id, 3), (other_root.id, 1), (empty_root.id, 1)):
        response = await client.get(f"{path}/{identity}/versions/{number}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "HYPOTHESIS_VERSION_NOT_FOUND"


async def test_hypothesis_reads_require_active_membership_in_default_workspace(
    hypotheses_client: AsyncClient, db_session: AsyncSession, workspace: Workspace
) -> None:
    client = hypotheses_client
    admin = await _user(db_session, UserRole.ADMIN)
    campaign = await _campaign(db_session, admin)
    other_workspace = Workspace(name="Other", slug="other")
    db_session.add(other_workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(
            workspace_id=other_workspace.id, user_id=admin.id, role=WorkspaceRole.WORKSPACE_ADMIN
        )
    )
    await db_session.flush()
    path = f"/api/v1/campaigns/{campaign.id}/hypotheses"
    detail = f"{path}/{uuid4()}/versions/1"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(detail)).status_code == 401
    _authenticate(client, admin)
    assert (await client.get(path)).status_code == 403
    assert (await client.get(detail)).status_code == 403

    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_id=admin.id,
        role=WorkspaceRole.WORKSPACE_ADMIN,
        is_active=False,
    )
    db_session.add(membership)
    await db_session.flush()
    assert (await client.get(path)).status_code == 403
    membership.is_active = True
    workspace.is_active = False
    await db_session.flush()
    assert (await client.get(path)).status_code == 403
    workspace.is_active = True
    await db_session.flush()
    response = await client.get(path)
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    assert response.json()["limit"] == 50
    assert response.json()["offset"] == 0
    missing = await client.get(f"/api/v1/campaigns/{uuid4()}/hypotheses")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CAMPAIGN_NOT_FOUND"

    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert (await client.get(path, params=params)).status_code == 422
    for number in (0, 2_147_483_648):
        assert (await client.get(f"{path}/{uuid4()}/versions/{number}")).status_code == 422
