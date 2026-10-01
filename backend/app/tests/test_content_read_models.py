from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.models.user import User, UserRole


async def test_content_list_exposes_approved_snapshot_and_article_provenance(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user = User(
        email=f"read-model-{uuid4()}@example.com",
        password_hash=hash_password("valid-password"),
        full_name="Read Model User",
        role=UserRole.ADMIN,
    )
    agent = Agent(
        name="Writer",
        slug=f"writer-{uuid4().hex[:8]}",
        role="content_writer",
        system_prompt="Write grounded content.",
        status=AgentStatus.ACTIVE,
        autonomy_level=1,
        settings={},
    )
    db_session.add_all([user, agent])
    await db_session.flush()
    campaign = Campaign(name="Read model campaign", goal="Test content cards", created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    task = Task(
        campaign_id=campaign.id,
        task_type=TaskType.WRITE_ARTICLE,
        title="Write source article",
        assigned_agent_id=agent.id,
        priority=TaskPriority.NORMAL,
        status=TaskStatus.COMPLETED,
        input_data={},
        output_data={},
        requires_approval=False,
        retry_count=0,
    )
    db_session.add(task)
    await db_session.flush()

    article = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.ARTICLE,
        title="Исходная статья",
        status=ContentStatus.APPROVED,
        author_agent_id=agent.id,
        metadata_={},
    )
    db_session.add(article)
    await db_session.flush()
    article_version = ContentVersion(
        content_item_id=article.id,
        version_number=1,
        content="Статья",
        structured_content={},
        created_by_agent_id=agent.id,
    )
    db_session.add(article_version)
    await db_session.flush()
    article.current_version_id = article_version.id

    pack = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.SOCIAL_POST_PACK,
        title="Пакет постов",
        status=ContentStatus.APPROVED,
        author_agent_id=agent.id,
        metadata_={},
    )
    db_session.add(pack)
    await db_session.flush()
    pack_version = ContentVersion(
        content_item_id=pack.id,
        version_number=1,
        content="Пакет",
        structured_content={},
        created_by_agent_id=agent.id,
    )
    db_session.add(pack_version)
    await db_session.flush()
    pack.current_version_id = pack_version.id

    post = ContentItem(
        campaign_id=campaign.id,
        source_task_id=task.id,
        content_type=ContentType.SOCIAL_POST,
        title="Пост из статьи",
        status=ContentStatus.APPROVED,
        author_agent_id=agent.id,
        parent_content_item_id=pack.id,
        metadata_={},
    )
    db_session.add(post)
    await db_session.flush()
    post_version = ContentVersion(
        content_item_id=post.id,
        version_number=1,
        content="Пост",
        structured_content={},
        created_by_agent_id=agent.id,
    )
    db_session.add(post_version)
    await db_session.flush()
    post.current_version_id = post_version.id
    db_session.add(
        ContentDerivation(
            derived_content_version_id=post_version.id,
            source_content_version_id=article_version.id,
            source_section_key="problem",
        )
    )
    db_session.add_all(
        [
            Approval(
                object_type=ApprovalObjectType.CONTENT_ITEM,
                object_id=article.id,
                subject_version=1,
                status=ApprovalStatus.APPROVED,
                subject_snapshot={"content_version_id": str(article_version.id)},
                metadata_={},
            ),
            Approval(
                object_type=ApprovalObjectType.CONTENT_ITEM,
                object_id=pack.id,
                subject_version=1,
                status=ApprovalStatus.APPROVED,
                subject_snapshot={
                    "content_version_id": str(pack_version.id),
                    "posts": [
                        {
                            "content_item_id": str(post.id),
                            "content_version_id": str(post_version.id),
                        }
                    ],
                },
                metadata_={},
            ),
        ]
    )
    await db_session.commit()

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "valid-password"},
    )
    assert response.status_code == 200
    listed = await client.get(f"/api/v1/content?campaign_id={campaign.id}")
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()}
    assert by_id[str(article.id)]["approved_version_id"] == str(article_version.id)
    assert by_id[str(article.id)]["approved_version_number"] == 1
    assert by_id[str(post.id)]["approved_version_id"] == str(post_version.id)
    assert by_id[str(post.id)]["source_content_item_id"] == str(article.id)
    assert by_id[str(post.id)]["source_content_item_title"] == "Исходная статья"

    detail = await client.get(f"/api/v1/content/{post.id}")
    assert detail.status_code == 200
    assert detail.json()["current_version"]["derivations"] == [
        {
            "source_content_item_id": str(article.id),
            "source_content_item_title": "Исходная статья",
            "source_content_version_id": str(article_version.id),
            "source_version_number": 1,
            "section_key": "problem",
        }
    ]
