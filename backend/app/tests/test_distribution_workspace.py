"""Contract and database regressions for the distribution read model.

Fixtures are committed deliberately: HTTP reads use a separate, read-only
transaction, just as they do in production.
"""

import base64
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.distribution_workspace import get_distribution_read_session
from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.core.errors import AppError
from app.core.security import encode_access_token
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.services.distribution_workspace_service import DistributionWorkspaceService
from app.services.publication_service import PublicationService

pytestmark = pytest.mark.integration
ENDPOINT = "/api/v1/distribution-workspace"


@dataclass
class CampaignFixture:
    user: User
    campaign: Campaign
    agent: Agent
    task: Task


@dataclass
class PostFixture:
    item: ContentItem
    version: ContentVersion
    approval: Approval | None


async def _campaign(
    session: AsyncSession,
    *,
    user: User | None = None,
    role: UserRole = UserRole.ADMIN,
    name: str = "Distribution campaign",
) -> CampaignFixture:
    if user is None:
        user = User(
            email=f"distribution-{uuid4()}@example.com",
            password_hash="not-used",
            full_name="Distribution operator",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
    agent = Agent(
        name="Fixture writer",
        slug=f"distribution-{uuid4().hex}",
        role="smm_manager",
        system_prompt="Fixture only",
        status=AgentStatus.ACTIVE,
        autonomy_level=0,
    )
    campaign = Campaign(
        name=name,
        goal="Preserve exact delivery evidence",
        status=CampaignStatus.ACTIVE,
        created_by=user.id,
    )
    session.add_all([agent, campaign])
    await session.flush()
    task = Task(
        campaign_id=campaign.id,
        assigned_agent_id=agent.id,
        task_type=TaskType.CREATE_SOCIAL_POSTS,
        title="Fixture social post",
        status=TaskStatus.COMPLETED,
    )
    session.add(task)
    await session.flush()
    return CampaignFixture(user, campaign, agent, task)


async def _version(
    session: AsyncSession,
    context: CampaignFixture,
    item: ContentItem,
    number: int,
    *,
    approved: bool = True,
) -> tuple[ContentVersion, Approval | None]:
    version = ContentVersion(
        content_item_id=item.id,
        version_number=number,
        content=f"Exact approved text v{number}",
        structured_content={"text": f"Exact approved text v{number}"},
        created_by_agent_id=context.agent.id,
    )
    session.add(version)
    await session.flush()
    item.current_version_id = version.id
    approval = None
    if approved:
        approval = Approval(
            object_type=ApprovalObjectType.CONTENT_ITEM,
            object_id=item.id,
            subject_version=number,
            status=ApprovalStatus.APPROVED,
            requested_by_agent_id=context.agent.id,
            reviewed_by_user_id=context.user.id,
            resolved_at=datetime.now(UTC),
            subject_snapshot={
                "content_item_id": str(item.id),
                "content_version_id": str(version.id),
                "version_number": number,
                "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
            },
        )
        session.add(approval)
        await session.flush()
    return version, approval


async def _post(
    session: AsyncSession,
    context: CampaignFixture,
    *,
    title: str = "Approved social post",
    approved: bool = True,
    metadata: dict[str, Any] | None = None,
) -> PostFixture:
    item = ContentItem(
        campaign_id=context.campaign.id,
        source_task_id=context.task.id,
        author_agent_id=context.agent.id,
        content_type=ContentType.SOCIAL_POST,
        status=ContentStatus.APPROVED,
        title=title,
        channel=ContentChannel.TELEGRAM,
        metadata_=metadata or {},
    )
    session.add(item)
    await session.flush()
    version, approval = await _version(session, context, item, 1, approved=approved)
    return PostFixture(item, version, approval)


async def _publication(
    session: AsyncSession,
    context: CampaignFixture,
    post: PostFixture,
    *,
    status: PublicationStatus = PublicationStatus.SCHEDULED,
    created_at: datetime | None = None,
    scheduled_at: datetime | None = None,
    failure_code: str | None = None,
) -> Publication:
    now = datetime.now(UTC)
    publication = Publication(
        campaign_id=context.campaign.id,
        content_item_id=post.item.id,
        content_version_id=post.version.id,
        channel=post.item.channel,
        status=status,
        scheduled_at=scheduled_at or now + timedelta(days=1),
        published_at=now if status is PublicationStatus.PUBLISHED else None,
        approved_for_publish_at=now,
        approved_for_publish_by=context.user.id,
        failure_code=failure_code,
        created_at=created_at or now,
        updated_at=created_at or now,
    )
    session.add(publication)
    await session.flush()
    return publication


async def _plan_item(
    session: AsyncSession,
    context: CampaignFixture,
    post: PostFixture,
    scheduled_at: datetime,
) -> PublicationPlanItem:
    article = await _post(session, context, title="Approved source article")
    article.item.content_type = ContentType.ARTICLE
    plan = PublicationPlan(
        campaign_id=context.campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=scheduled_at - timedelta(days=1),
        planning_horizon_end=scheduled_at + timedelta(days=1),
        created_by_user_id=context.user.id,
        approved_at=datetime.now(UTC),
        approved_by_user_id=context.user.id,
    )
    session.add(plan)
    await session.flush()
    planned = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=1,
        scheduled_at=scheduled_at,
        channel=post.item.channel,
        source_content_item_id=article.item.id,
        source_content_version_id=article.version.id,
        topic="Evidence-backed post",
        angle="Exact version",
        purpose="Explain the approved article",
        format="post",
        message_brief="Use the cited article version",
        status=PublicationPlanItemStatus.PLANNED,
    )
    session.add(planned)
    await session.flush()
    post.item.metadata_ = {
        "publication_plan_id": str(plan.id),
        "publication_plan_item_id": str(planned.id),
        "source_content_version_id": str(article.version.id),
    }
    post.version.generation_key = f"plan-item:{planned.id}"
    session.add(
        ContentDerivation(
            derived_content_version_id=post.version.id,
            source_content_version_id=article.version.id,
            source_section_key="argument",
        )
    )
    return planned


@asynccontextmanager
async def _read_session() -> AsyncIterator[AsyncSession]:
    dependency = get_distribution_read_session()
    try:
        yield await anext(dependency)
    finally:
        await dependency.aclose()


async def _workspace(
    user: User, *, campaign_id: UUID | None = None, limit: int = 50, cursor: str | None = None
) -> dict[str, Any]:
    async with _read_session() as session:
        result = await DistributionWorkspaceService(session).get(
            user, campaign_id=campaign_id, limit=limit, cursor=cursor
        )
        return result.model_dump(mode="json")


def _authenticate(client: AsyncClient, user: User) -> None:
    client.cookies.set(settings.auth_cookie_name, encode_access_token(user.id, user.auth_version))


def _by_publication(response: dict[str, Any], publication: Publication) -> dict[str, Any]:
    return next(
        row for row in response["items"] if row.get("publication_id") == str(publication.id)
    )


async def test_scheduled_version_survives_a_new_approved_current_version(db_session):
    context = await _campaign(db_session)
    post = await _post(db_session, context)
    publication = await _publication(db_session, context, post)
    newer, _ = await _version(db_session, context, post.item, 2)
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    card = _by_publication(result, publication)
    assert card["content_version_id"] == str(post.version.id)
    assert card["content_version_id"] != str(newer.id)
    assert card["sections"] == ["upcoming"]
    assert all(item["kind"] == "publication" for item in result["items"])

    # The read model does not alter PR66's exact-version command contract.
    replanned = await PublicationService(db_session).replan(
        publication.id, datetime.now(UTC) + timedelta(days=3), context.user
    )
    assert replanned.content_version_id == post.version.id


async def test_only_an_exact_approved_unchanged_current_version_is_a_candidate(db_session):
    context = await _campaign(db_session)
    ready = await _post(db_session, context, title="Exact current approval")
    outdated = await _post(db_session, context, title="Only older version approved")
    await _version(db_session, context, outdated.item, 2, approved=False)
    await _post(db_session, context, title="No approval", approved=False)
    modified = await _post(db_session, context, title="Changed after approval")
    modified.version.content = "Modified after the immutable approval snapshot"
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    assert [(row["kind"], row["id"]) for row in result["items"]] == [
        ("candidate", str(ready.item.id))
    ]
    card = result["items"][0]
    assert card["approved_version_id"] == str(ready.version.id)
    assert card["approved_version_number"] == 1
    assert card["state"] == "needs_plan"
    assert card["allowed_actions"] == []
    assert result["counts"]["needs_plan"] == 1


async def test_exact_cancelled_version_does_not_reappear_as_a_candidate(db_session):
    context = await _campaign(db_session)
    cancelled_exact = await _post(db_session, context)
    await _publication(db_session, context, cancelled_exact, status=PublicationStatus.CANCELLED)
    cancelled_old = await _post(db_session, context)
    await _publication(db_session, context, cancelled_old, status=PublicationStatus.CANCELLED)
    current, _ = await _version(db_session, context, cancelled_old.item, 2)
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    candidates = [row for row in result["items"] if row["kind"] == "candidate"]
    assert [row["id"] for row in candidates] == [str(cancelled_old.item.id)]
    assert candidates[0]["approved_version_id"] == str(current.id)
    assert result["counts"]["cancelled_history"] == 2


async def test_parent_pack_approval_requires_the_exact_child_and_version(db_session):
    context = await _campaign(db_session)
    parent = await _post(db_session, context, title="Approved social pack")
    parent.item.content_type = ContentType.SOCIAL_POST_PACK
    approved_child = await _post(db_session, context, title="Approved child", approved=False)
    wrong_child = await _post(db_session, context, title="Not approved child", approved=False)
    approved_child.item.parent_content_item_id = parent.item.id
    wrong_child.item.parent_content_item_id = parent.item.id
    assert parent.approval is not None
    parent.approval.subject_snapshot = {
        "posts": [
            {
                "content_item_id": str(approved_child.item.id),
                "content_version_id": str(approved_child.version.id),
                "content_hash": hashlib.sha256(approved_child.version.content.encode()).hexdigest(),
            },
            {
                "content_item_id": str(wrong_child.item.id),
                "content_version_id": str(approved_child.version.id),
            },
        ]
    }
    await db_session.commit()

    result = await _workspace(context.user)

    assert [row["id"] for row in result["items"]] == [str(approved_child.item.id)]
    assert result["items"][0]["approved_version_id"] == str(approved_child.version.id)


async def test_plan_candidate_and_scheduled_publication_use_distinct_lateness_rules(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "publication_auto_dispatch_max_lateness_seconds", 300)
    context = await _campaign(db_session)
    now = datetime.now(UTC)
    ready = await _post(db_session, context, title="Future approved slot")
    await _plan_item(db_session, context, ready, now + timedelta(days=1))
    overdue = await _post(db_session, context, title="Expired approved slot")
    await _plan_item(db_session, context, overdue, now - timedelta(minutes=1))
    in_grace = await _publication(
        db_session,
        context,
        await _post(db_session, context),
        scheduled_at=now - timedelta(minutes=1),
    )
    late = await _publication(
        db_session,
        context,
        await _post(db_session, context),
        scheduled_at=now - timedelta(minutes=10),
    )
    await db_session.commit()

    result = await _workspace(context.user)

    candidates = {row["id"]: row for row in result["items"] if row["kind"] == "candidate"}
    assert candidates[str(ready.item.id)]["state"] == "ready"
    assert candidates[str(ready.item.id)]["allowed_actions"] == []
    assert candidates[str(overdue.item.id)]["state"] == "overdue"
    assert set(candidates[str(overdue.item.id)]["allowed_actions"]) == {
        "reschedule_plan",
        "cancel_plan",
    }
    assert _by_publication(result, in_grace)["is_overdue"] is False
    late_card = _by_publication(result, late)
    assert late_card["is_overdue"] is True
    as_of = datetime.fromisoformat(result["as_of"])
    assert late_card["lateness_seconds"] == int((as_of - late.scheduled_at).total_seconds())


async def test_foreign_campaign_plan_metadata_cannot_enable_candidate_actions(db_session):
    context = await _campaign(db_session)
    other = await _campaign(db_session, user=context.user)
    local = await _post(db_session, context)
    foreign = await _post(db_session, other)
    planned = await _plan_item(db_session, other, foreign, datetime.now(UTC) - timedelta(days=1))
    local.item.metadata_ = dict(foreign.item.metadata_)
    local.version.generation_key = f"plan-item:{planned.id}"
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    assert len(result["items"]) == 1
    card = result["items"][0]
    assert card["id"] == str(local.item.id)
    assert card["state"] == "needs_plan"
    assert card["allowed_actions"] == []
    assert card["plan_scheduled_at"] is None


async def test_published_fact_and_every_cancelled_history_row_are_preserved(db_session):
    context = await _campaign(db_session)
    post = await _post(db_session, context)
    older = await _publication(db_session, context, post, status=PublicationStatus.CANCELLED)
    newer = await _publication(db_session, context, post, status=PublicationStatus.CANCELLED)
    published = await _publication(db_session, context, post, status=PublicationStatus.PUBLISHED)
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    assert _by_publication(result, published)["sections"] == ["published"]
    for publication in [older, newer]:
        card = _by_publication(result, publication)
        assert card["sections"] == ["cancelled_history"]
        assert card["has_replacement"] is True
        assert "replan" not in card["allowed_actions"]
    assert result["counts"]["published"] == 1
    assert result["counts"]["cancelled_history"] == 2
    assert result["counts"]["cancelled"] == 0


async def test_replacement_on_another_page_still_suppresses_cancelled_actions(db_session):
    context = await _campaign(db_session)
    post = await _post(db_session, context, title="Original")
    now = datetime.now(UTC)
    original = await _publication(
        db_session, context, post, status=PublicationStatus.CANCELLED, created_at=now
    )
    copy = await _post(
        db_session,
        context,
        title="Separate approved copy",
        metadata={"reuse_source_content_id": str(post.item.id)},
    )
    replacement = await _publication(db_session, context, copy, created_at=now - timedelta(days=1))
    await db_session.commit()

    first = await _workspace(context.user, campaign_id=context.campaign.id, limit=1)

    assert len(first["items"]) == 1
    card = _by_publication(first, original)
    assert card["has_replacement"] is True
    assert card["sections"] == ["cancelled_history"]
    assert card["allowed_actions"] == []
    assert first["pagination"]["has_more"] is True
    second = await _workspace(
        context.user,
        campaign_id=context.campaign.id,
        limit=1,
        cursor=first["pagination"]["next_cursor"],
    )
    assert _by_publication(second, replacement)["sections"] == ["upcoming"]


async def test_cancelled_representative_is_global_and_grouped_by_identity(db_session):
    context = await _campaign(db_session)
    post = await _post(db_session, context, title="Identical title")
    other = await _post(db_session, context, title="Identical title")
    now = datetime.now(UTC)
    historical = await _publication(
        db_session,
        context,
        post,
        status=PublicationStatus.CANCELLED,
        created_at=now,
        scheduled_at=now - timedelta(days=3),
    )
    representative = await _publication(
        db_session,
        context,
        post,
        status=PublicationStatus.CANCELLED,
        created_at=now - timedelta(days=1),
        scheduled_at=now - timedelta(days=2),
    )
    other_cancelled = await _publication(
        db_session, context, other, status=PublicationStatus.CANCELLED
    )
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    assert _by_publication(result, historical)["sections"] == ["cancelled_history"]
    assert _by_publication(result, historical)["allowed_actions"] == []
    for publication in [representative, other_cancelled]:
        assert set(_by_publication(result, publication)["sections"]) == {
            "cancelled",
            "cancelled_history",
        }
    assert result["counts"]["cancelled"] == 2
    assert result["counts"]["cancelled_history"] == 3


async def test_publishing_and_unknown_delivery_never_offer_a_blind_retry(db_session, monkeypatch):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    context = await _campaign(db_session)
    publishing = await _publication(
        db_session, context, await _post(db_session, context), status=PublicationStatus.PUBLISHING
    )
    unknown = await _publication(
        db_session,
        context,
        await _post(db_session, context),
        status=PublicationStatus.FAILED,
        failure_code="TELEGRAM_RECONCILIATION_REQUIRED",
    )
    retryable = await _publication(
        db_session,
        context,
        await _post(db_session, context),
        status=PublicationStatus.FAILED,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=10),
        failure_code="TELEGRAM_RATE_LIMIT",
    )
    await db_session.commit()

    result = await _workspace(context.user, campaign_id=context.campaign.id)

    assert _by_publication(result, publishing)["allowed_actions"] == []
    uncertain = _by_publication(result, unknown)
    assert uncertain["sections"] == ["attention"]
    assert set(uncertain["allowed_actions"]) == {"reconcile_published", "reconcile_not_published"}
    assert "retry" in _by_publication(result, retryable)["allowed_actions"]


async def test_failed_rate_limit_can_retry_with_a_retained_execution_token(db_session, monkeypatch):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    context = await _campaign(db_session)
    publication = await _publication(
        db_session,
        context,
        await _post(db_session, context),
        status=PublicationStatus.FAILED,
        scheduled_at=datetime.now(UTC) - timedelta(minutes=10),
        failure_code="TELEGRAM_RATE_LIMIT",
    )
    # Provider execution retains its attempt token on failure. The existing
    # explicit retry command clears that token before claiming a new attempt.
    previous_token = str(uuid4())
    publication.execution_token = previous_token
    await db_session.commit()

    card = _by_publication(await _workspace(context.user), publication)

    assert card["sections"] == ["attention"]
    assert "retry" in card["allowed_actions"]
    assert "publish_now" not in card["allowed_actions"]
    await db_session.refresh(publication)
    assert publication.execution_token == previous_token
    assert publication.status is PublicationStatus.FAILED


@pytest.mark.parametrize("missing_plan_item", ["valid-uuid", "malformed-uuid"])
async def test_missing_plan_with_an_override_audit_is_safe_and_non_actionable(
    client, db_session, missing_plan_item
):
    context = await _campaign(db_session)
    plan_item_id = str(uuid4()) if missing_plan_item == "valid-uuid" else "not-a-uuid"
    post = await _post(
        db_session,
        context,
        metadata={
            "publication_plan_id": str(uuid4()),
            "publication_plan_item_id": plan_item_id,
            "source_content_version_id": str(uuid4()),
        },
    )
    publication = await _publication(db_session, context, post)
    db_session.add(
        ActivityLog(
            campaign_id=context.campaign.id,
            content_item_id=post.item.id,
            user_id=context.user.id,
            event_type="PUBLICATION_PLAN_SLOT_RESCHEDULED",
            operation_key=f"publication-plan-slot-rescheduled:{publication.id}",
            metadata_={
                "publication_id": str(publication.id),
                "publication_plan_item_id": plan_item_id,
                "content_version_id": str(post.version.id),
                "channel": post.item.channel.value,
                "original_scheduled_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "effective_scheduled_at": publication.scheduled_at.isoformat(),
            },
        )
    )
    await db_session.commit()
    _authenticate(client, context.user)

    response = await client.get(ENDPOINT, params={"campaign_id": str(context.campaign.id)})

    assert response.status_code == 200
    card = _by_publication(response.json(), publication)
    assert card["publication_plan_item_id"] is None
    assert card["content_version_id"] == str(post.version.id)
    assert card["allowed_actions"] == ["cancel"]


async def test_archived_material_is_cancellation_only(db_session):
    context = await _campaign(db_session)
    post = await _post(db_session, context)
    publication = await _publication(
        db_session, context, post, status=PublicationStatus.WAITING_APPROVAL
    )
    post.item.status = ContentStatus.ARCHIVED
    await db_session.commit()

    card = _by_publication(await _workspace(context.user), publication)

    assert card["content_source_archived"] is True
    assert card["allowed_actions"] == ["cancel"]


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
async def test_authenticated_roles_keep_legacy_read_access_and_campaign_scope(
    client, db_session, role
):
    first = await _campaign(db_session, role=role, name="Selected campaign")
    selected = await _publication(db_session, first, await _post(db_session, first))
    # A different author is not a security boundary in the current single-workspace runtime.
    second = await _campaign(db_session, name="Other author campaign")
    outside = await _publication(db_session, second, await _post(db_session, second))
    await db_session.commit()
    _authenticate(client, first.user)

    response = await client.get(ENDPOINT, params={"campaign_id": str(first.campaign.id)})

    assert response.status_code == 200
    result = response.json()
    assert result["campaign"]["id"] == str(first.campaign.id)
    assert [row["publication_id"] for row in result["items"]] == [str(selected.id)]
    assert str(outside.id) not in response.text
    assert result["counts"]["upcoming"] == 1
    global_response = await client.get(ENDPOINT)
    assert global_response.status_code == 200
    assert {row["publication_id"] for row in global_response.json()["items"]} == {
        str(selected.id),
        str(outside.id),
    }


async def test_authentication_and_active_user_are_required(client, db_session):
    assert (await client.get(ENDPOINT)).status_code == 401
    context = await _campaign(db_session)
    context.user.is_active = False
    await db_session.commit()
    _authenticate(client, context.user)
    assert (await client.get(ENDPOINT)).status_code == 403


@pytest.mark.parametrize("change", ["revoke", "deactivate"])
async def test_read_snapshot_rechecks_a_previously_authenticated_user(db_session, change):
    context = await _campaign(db_session)
    await _post(db_session, context)
    await db_session.commit()
    # Keep the object returned by authentication stale while another connection changes it.
    values = (
        {"auth_version": context.user.auth_version + 1}
        if change == "revoke"
        else {"is_active": False}
    )
    async with async_session_factory() as writer:
        await writer.execute(update(User).where(User.id == context.user.id).values(**values))
        await writer.commit()

    with pytest.raises(AppError) as denied:
        await _workspace(context.user)

    assert denied.value.status_code == (401 if change == "revoke" else 403)


async def test_cursor_pages_have_no_duplicates_and_counts_describe_only_the_page(db_session):
    context = await _campaign(db_session)
    tied_time = datetime.now(UTC)
    expected = set()
    for _ in range(5):
        publication = await _publication(
            db_session,
            context,
            await _post(db_session, context),
            created_at=tied_time,
        )
        expected.add(str(publication.id))
    await db_session.commit()

    cursor = None
    seen = []
    page_lengths = []
    for _ in range(3):
        result = await _workspace(
            context.user, campaign_id=context.campaign.id, limit=2, cursor=cursor
        )
        page_lengths.append(len(result["items"]))
        seen.extend(row["publication_id"] for row in result["items"])
        assert result["counts"]["upcoming"] == len(result["items"])
        assert result["pagination"]["limit"] == 2
        assert datetime.fromisoformat(result["as_of"]).tzinfo is not None
        cursor = result["pagination"]["next_cursor"]
        assert result["pagination"]["has_more"] is (cursor is not None)

    assert page_lengths == [2, 2, 1]
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == expected
    assert cursor is None


async def test_cursor_cannot_be_reused_for_another_campaign(client, db_session):
    first = await _campaign(db_session)
    for _ in range(2):
        await _publication(db_session, first, await _post(db_session, first))
    second = await _campaign(db_session, user=first.user)
    await db_session.commit()
    _authenticate(client, first.user)
    page = await client.get(ENDPOINT, params={"campaign_id": str(first.campaign.id), "limit": 1})
    assert page.status_code == 200
    cursor = page.json()["pagination"]["next_cursor"]
    assert cursor

    wrong_scope = await client.get(
        ENDPOINT, params={"campaign_id": str(second.campaign.id), "limit": 1, "cursor": cursor}
    )
    assert wrong_scope.status_code == 422


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "invalid"},
        pytest.param(
            {
                "cursor": base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "v": 1,
                            "scope": None,
                            "created_at": "2026-11-22T12:00:00+00:00",
                            "kind": "publication",
                            "id": 1,
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            },
            id="numeric-cursor-id",
        ),
        pytest.param(
            {
                "cursor": base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "v": 1,
                            "scope": None,
                            "created_at": "0001-01-01T00:00:00+01:00",
                            "kind": "publication",
                            "id": "00000000-0000-0000-0000-000000000001",
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            },
            id="cursor-timezone-underflow",
        ),
    ],
)
async def test_invalid_page_parameters_are_rejected(client, db_session, parameters):
    context = await _campaign(db_session)
    await db_session.commit()
    _authenticate(client, context.user)
    assert (await client.get(ENDPOINT, params=parameters)).status_code == 422


async def test_query_count_does_not_grow_with_publications_or_campaigns(db_session):
    context = await _campaign(db_session)
    await _publication(db_session, context, await _post(db_session, context))
    await db_session.commit()
    queries: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            queries.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await _workspace(context.user, limit=5)
        small_count = len(queries)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    for index in range(20):
        scope = context if index % 2 else await _campaign(db_session, user=context.user)
        await _publication(db_session, scope, await _post(db_session, scope))
    await db_session.commit()
    queries.clear()
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await _workspace(context.user, limit=5)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert len(result["items"]) == 5
    assert result["pagination"]["has_more"] is True
    assert 0 < small_count <= 16
    assert len(queries) <= small_count + 2


async def test_read_only_transaction_holds_one_snapshot_across_concurrent_writes(db_session):
    context = await _campaign(db_session)
    publication = await _publication(db_session, context, await _post(db_session, context))
    await db_session.commit()

    async with _read_session() as reader:
        assert await reader.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await reader.scalar(text("SHOW transaction_read_only")) == "on"
        service = DistributionWorkspaceService(reader)
        before = (await service.get(context.user)).model_dump(mode="json")
        async with async_session_factory() as writer:
            await writer.execute(
                update(Publication)
                .where(Publication.id == publication.id)
                .values(status=PublicationStatus.PUBLISHED, published_at=datetime.now(UTC))
            )
            await writer.commit()
        during = (await service.get(context.user)).model_dump(mode="json")

    after = await _workspace(context.user)
    assert _by_publication(before, publication)["status"] == "SCHEDULED"
    assert _by_publication(during, publication)["status"] == "SCHEDULED"
    assert _by_publication(after, publication)["status"] == "PUBLISHED"


async def test_read_session_rejects_writes_at_the_database_boundary(db_session):
    context = await _campaign(db_session)
    await db_session.commit()

    async with _read_session() as reader:
        with pytest.raises(DBAPIError, match="read-only"):
            await reader.execute(
                update(Campaign).where(Campaign.id == context.campaign.id).values(name="Forbidden")
            )

    await db_session.refresh(context.campaign)
    assert context.campaign.name == "Distribution campaign"


async def test_get_has_no_writes_locks_or_provider_side_effects(db_session, monkeypatch):
    from app.integrations.telegram import TelegramProvider, VKProvider

    context = await _campaign(db_session)
    await _publication(db_session, context, await _post(db_session, context))
    await db_session.commit()
    before_audit = await db_session.scalar(select(func.count()).select_from(ActivityLog))
    statements: list[str] = []

    async def forbidden(*_args, **_kwargs):
        pytest.fail("A distribution GET must not invoke a writer or external provider")

    monkeypatch.setattr(TelegramProvider, "publish", forbidden)
    monkeypatch.setattr(VKProvider, "publish", forbidden)
    monkeypatch.setattr(PublicationService, "recover_stuck_publishing", forbidden)

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await _workspace(context.user)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert result["items"]
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
    assert not any("FOR UPDATE" in statement for statement in statements)
    assert await db_session.scalar(select(func.count()).select_from(ActivityLog)) == before_audit


async def test_stale_allowed_action_does_not_authorize_a_changed_publication(db_session):
    context = await _campaign(db_session)
    publication = await _publication(db_session, context, await _post(db_session, context))
    await db_session.commit()
    original = _by_publication(await _workspace(context.user), publication)
    assert "cancel" in original["allowed_actions"]

    publication.status = PublicationStatus.PUBLISHED
    publication.published_at = datetime.now(UTC)
    await db_session.commit()

    with pytest.raises(AppError):
        await PublicationService(db_session).cancel(publication.id, context.user)
    await db_session.refresh(publication)
    assert publication.status is PublicationStatus.PUBLISHED
