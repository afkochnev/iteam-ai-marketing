import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.content import approve_content, reject_content
from app.core.database import async_session_factory
from app.core.errors import AppError
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus, AgentTool
from app.models.agent_run import AgentRunStatus, ToolCall, ToolCallStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.schemas.campaign import CampaignCreate
from app.schemas.content import ContentApprovalRequest, ContentRejectionRequest
from app.schemas.task import TaskCreate
from app.services.agent_run_service import AgentRunService
from app.services.agent_runner_service import RuntimeResult
from app.services.approval_service import ApprovalService
from app.services.campaign_service import CampaignService
from app.services.task_result_processors import result_processor_registry
from app.services.task_service import TaskService


def social_result(version_id: str, *, sufficient: bool = True) -> dict[str, object]:
    if not sufficient:
        return {
            "sufficient": False,
            "pack": None,
            "gaps": ["Недостаточно подтверждённых материалов."],
        }
    posts = [
        {
            "key": f"post_{index}",
            "channel": "TELEGRAM" if index % 2 else "VK",
            "title": f"Пост {index}",
            "text_markdown": f"Подтверждённый текст {index}",
            "cta": "Узнать больше",
            "sources": [{"content_version_id": version_id, "section_key": "problem"}],
            "suggested_publish_order": index,
        }
        for index in range(1, 6)
    ]
    return {"sufficient": True, "pack": {"strategy_summary": "Контентная серия", "posts": posts}}


async def smm_fixture(session: AsyncSession) -> tuple[Task, Campaign, ContentVersion]:
    owner = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Owner",
        role=UserRole.ADMIN,
    )
    writer = Agent(
        name="Writer",
        slug="writer",
        role="content_writer",
        system_prompt="Write grounded content.",
        model="test-model",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    smm = Agent(
        name="SMM Manager",
        slug="smm_manager",
        role="smm_manager",
        system_prompt="Adapt the article.",
        model="test-model",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    session.add_all([writer, smm])
    await session.flush()
    session.add(AgentTool(agent_id=smm.id, tool_name="read_content_version", is_enabled=True))
    campaign = await CampaignService(session).create_campaign(
        CampaignCreate(name="SMM integration campaign", goal="Create social content"), owner
    )
    campaign.status = CampaignStatus.ACTIVE
    campaign.strategy = {
        "social_strategy": {"channels": ["TELEGRAM", "VK"], "post_count": 5},
    }
    article_task = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.WRITE_ARTICLE,
            title="Approved article",
            assigned_agent_id=writer.id,
        )
    )
    article_task.status = TaskStatus.COMPLETED
    article_item = ContentItem(
        campaign_id=campaign.id,
        source_task_id=article_task.id,
        content_type=ContentType.ARTICLE,
        title="Approved article",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=writer.id,
        metadata_={},
    )
    session.add(article_item)
    await session.flush()
    article_version = ContentVersion(
        content_item_id=article_item.id,
        version_number=1,
        content="# Article",
        structured_content={
            "title": "Approved article",
            "lead": "Lead",
            "sections": [{"key": "problem", "heading": "Problem", "body_markdown": "Evidence"}],
            "conclusion": "Conclusion",
            "cta": "CTA",
        },
        created_by_agent_id=writer.id,
        generation_key="article",
    )
    session.add(article_version)
    await session.flush()
    article_item.current_version_id = article_version.id
    article_task.output_data = {
        "content_item_id": str(article_item.id),
        "content_version_id": str(article_version.id),
        "content_type": "ARTICLE",
    }
    social_task = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            task_type=TaskType.CREATE_SOCIAL_POSTS,
            title="Create posts",
            assigned_agent_id=smm.id,
            dependency_ids=[article_task.id],
            input_data={"brief": "Adapt article into social posts"},
        )
    )
    await session.commit()
    return social_task, campaign, article_version


async def add_read_audit(session: AsyncSession, run_id: UUID, version: ContentVersion) -> None:
    session.add(
        ToolCall(
            agent_run_id=run_id,
            tool_name="read_content_version",
            arguments={"content_version_id": str(version.id)},
            result={
                "content_version_id": str(version.id),
                "title": "Approved article",
                "section_keys": ["problem"],
                "content_hash": "hash",
            },
            status=ToolCallStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_smm_success_persists_pack_posts_versions_and_derivations(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.COMPLETED
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    items = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.source_task_id == task.id)
            )
        ).all()
    )
    packs = [item for item in items if item.content_type is ContentType.SOCIAL_POST_PACK]
    posts = [item for item in items if item.content_type is ContentType.SOCIAL_POST]
    assert len(packs) == 1 and len(posts) == 5
    pack = packs[0]
    assert pack.status is ContentStatus.WAITING_APPROVAL
    assert all(post.status is ContentStatus.WAITING_APPROVAL for post in posts)
    assert all(post.parent_content_item_id == pack.id for post in posts)
    assert all(len(post.versions) == 1 for post in posts)
    assert len(pack.versions) == 1
    assert all(version.source_agent_run_id == run.id for post in posts for version in post.versions)
    derivations = list((await db_session.scalars(select(ContentDerivation))).all())
    assert len(derivations) == 5
    assert all(item.source_content_version_id == article_version.id for item in derivations)
    approval_count = await db_session.scalar(
        select(func.count())
        .select_from(Approval)
        .where(
            Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    assert approval_count == 1
    run_row = await service.get_run(run.id)
    task_row = await TaskService(db_session).get_task(task.id)
    await result_processor_registry.get(task_row.task_type).process(
        db_session, run_row, task_row, social_result(str(article_version.id))
    )
    await db_session.commit()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.source_task_id == task.id)
        )
        == 6
    )
    assert await db_session.scalar(select(func.count()).select_from(ContentDerivation)) == 5


@pytest.mark.asyncio
async def test_smm_without_read_fails_before_persisting_content(db_session: AsyncSession) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None),
    )
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.FAILED
    failed_run = await service.get_run(run.id)
    assert failed_run.error_code == "CONTENT_VERSION_NOT_READ"
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
async def test_smm_insufficient_completes_run_but_fails_task_without_content(
    db_session: AsyncSession,
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id,
        RuntimeResult(social_result(str(article_version.id), sufficient=False), 1, 10, 5, 15, None),
    )
    assert (await service.get_run(run.id)).status is AgentRunStatus.COMPLETED
    stored_task = await TaskService(db_session).get_task(task.id)
    assert stored_task.status is TaskStatus.FAILED
    assert stored_task.output_data["error_code"] == "INSUFFICIENT_SOCIAL_SOURCE"
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unknown_version", "unknown_section"])
async def test_smm_rejects_hallucinated_social_provenance(
    db_session: AsyncSession, mode: str
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    output = social_result(str(article_version.id))
    pack = output["pack"]
    assert isinstance(pack, dict)
    posts = pack["posts"]
    assert isinstance(posts, list) and posts
    source = posts[0]["sources"][0]
    if mode == "unknown_version":
        source["content_version_id"] = str(uuid4())
    else:
        source["section_key"] = "imaginary_section"
    await service.finish_success(run.id, RuntimeResult(output, 1, 10, 5, 15, None))
    failed_run = await service.get_run(run.id)
    assert failed_run.status is AgentRunStatus.FAILED
    assert failed_run.error_code in {"INVALID_SOCIAL_SOURCE", "INVALID_SOCIAL_SOURCE_SECTION"}
    assert await db_session.scalar(select(func.count()).select_from(ContentItem)) == 1


@pytest.mark.asyncio
async def test_pack_approval_is_safe_for_concurrent_requests(db_session: AsyncSession) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id, RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None)
    )
    pack = await db_session.scalar(
        select(ContentItem).where(
            ContentItem.source_task_id == task.id,
            ContentItem.content_type == ContentType.SOCIAL_POST_PACK,
        )
    )
    user = await db_session.scalar(select(User))
    assert pack is not None and user is not None
    pack_id = pack.id
    user_id = user.id

    async def approve_once() -> None:
        async with async_session_factory() as session:
            approver = await session.get(User, user_id)
            assert approver is not None
            await approve_content(
                pack_id, ContentApprovalRequest(comment="Согласовано"), approver, session
            )

    results = await asyncio.gather(approve_once(), approve_once(), return_exceptions=True)
    assert all(not isinstance(result, Exception) for result in results)
    await db_session.refresh(pack)
    assert pack.status is ContentStatus.APPROVED
    children = list(
        (
            await db_session.scalars(
                select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
            )
        ).all()
    )
    assert len(children) == 5
    assert all(child.status is ContentStatus.APPROVED for child in children)
    approvals = list(
        (
            await db_session.scalars(
                select(Approval).where(
                    Approval.object_id == pack.id,
                    Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                )
            )
        ).all()
    )
    assert len(approvals) == 1 and approvals[0].status is ApprovalStatus.APPROVED


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["pack", "child"])
async def test_pack_approval_rejects_stale_pack_or_child_version(
    db_session: AsyncSession, target: str
) -> None:
    task, _campaign, article_version = await smm_fixture(db_session)
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id, RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None)
    )
    pack = await db_session.scalar(
        select(ContentItem).where(
            ContentItem.source_task_id == task.id,
            ContentItem.content_type == ContentType.SOCIAL_POST_PACK,
        )
    )
    user = await db_session.scalar(select(User))
    assert pack is not None and user is not None and pack.current_version_id is not None
    if target == "pack":
        replacement = ContentVersion(
            content_item_id=pack.id,
            version_number=2,
            content="new pack",
            structured_content={},
            created_by_agent_id=pack.author_agent_id,
            generation_key="pack-replacement",
        )
    else:
        child = await db_session.scalar(
            select(ContentItem).where(ContentItem.parent_content_item_id == pack.id)
        )
        assert child is not None
        replacement = ContentVersion(
            content_item_id=child.id,
            version_number=2,
            content="new post",
            structured_content={},
            created_by_agent_id=child.author_agent_id,
            generation_key="post-replacement",
        )
    db_session.add(replacement)
    await db_session.flush()
    if target == "pack":
        pack.current_version_id = replacement.id
    else:
        assert child is not None
        child.current_version_id = replacement.id
    await db_session.commit()
    with pytest.raises(AppError) as error:
        await approve_content(
            pack.id, ContentApprovalRequest(comment="Согласовано"), user, db_session
        )
    assert error.value.code == "CONTENT_APPROVAL_STALE"


@pytest.mark.asyncio
async def test_smm_requires_read_for_every_allowed_article_version(
    db_session: AsyncSession,
) -> None:
    task, campaign, article_version = await smm_fixture(db_session)
    second_item = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.ARTICLE,
        title="Second article",
        status=ContentStatus.WAITING_APPROVAL,
        author_agent_id=task.assigned_agent_id,
        metadata_={},
    )
    db_session.add(second_item)
    await db_session.flush()
    second_version = ContentVersion(
        content_item_id=second_item.id,
        version_number=1,
        content="# Second",
        structured_content={"sections": []},
        created_by_agent_id=task.assigned_agent_id,
        generation_key="second-article",
    )
    db_session.add(second_version)
    await db_session.flush()
    second_item.current_version_id = second_version.id
    service = AgentRunService(db_session)
    run = await service.create_queued_run(task.id)
    assert await service.claim(run.id)
    run.input_data["allowed_content_version_ids"] = [
        str(article_version.id),
        str(second_version.id),
    ]
    await add_read_audit(db_session, run.id, article_version)
    await db_session.commit()
    await service.finish_success(
        run.id, RuntimeResult(social_result(str(article_version.id)), 1, 10, 5, 15, None)
    )
    failed_run = await service.get_run(run.id)
    assert failed_run.error_code == "CONTENT_VERSION_NOT_READ"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(ContentItem.source_task_id == task.id)
        )
        == 1
    )


async def create_article_approval(session: AsyncSession) -> tuple[ContentItem, User]:
    article = await session.scalar(
        select(ContentItem).where(ContentItem.content_type == ContentType.ARTICLE)
    )
    user = await session.scalar(select(User))
    assert article is not None and user is not None and article.current_version_id is not None
    version = next(
        version for version in article.versions if version.id == article.current_version_id
    )
    await ApprovalService(session).create_content_approval(
        article.id,
        version.version_number,
        {
            "content_item_id": str(article.id),
            "content_version_id": str(version.id),
            "version_number": version.version_number,
            "title": article.title,
            "content_hash": "hash",
        },
        article.author_agent_id,
    )
    await session.commit()
    return article, user


@pytest.mark.asyncio
async def test_article_approval_can_approve_and_reject_with_authenticated_user(
    db_session: AsyncSession,
) -> None:
    await smm_fixture(db_session)
    article, user = await create_article_approval(db_session)
    approved = await approve_content(
        article.id, ContentApprovalRequest(comment="Согласовано"), user, db_session
    )
    assert approved.status is ContentStatus.APPROVED
    article, user = await create_article_approval(db_session)
    with pytest.raises(AppError):
        await reject_content(article.id, ContentRejectionRequest(comment=" "), user, db_session)
