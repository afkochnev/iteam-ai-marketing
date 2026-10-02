from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentStatus
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.knowledge_pack import KnowledgePack, KnowledgePackStatus
from app.models.publication import Publication, PublicationStatus
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.services.campaign_workspace_service import CampaignWorkspaceService


async def workspace_fixture(
    session: AsyncSession, *, with_post: bool = True, with_failure: bool = False
) -> dict[str, object]:
    user = User(
        email=f"workspace-{uuid4()}@example.com",
        password_hash="not-used-in-this-test",
        full_name="Workspace User",
        role=UserRole.ADMIN,
    )
    writer = Agent(
        name="Writer",
        slug="writer",
        role="content_writer",
        description="Writes articles",
        system_prompt="Write articles",
        status=AgentStatus.ACTIVE,
        autonomy_level=1,
    )
    keeper = Agent(
        name="Knowledge Keeper",
        slug="knowledge_keeper",
        role="knowledge_keeper",
        description="Researches knowledge",
        system_prompt="Research knowledge",
        status=AgentStatus.ACTIVE,
        autonomy_level=1,
    )
    smm = Agent(
        name="SMM Manager",
        slug="smm_manager",
        role="smm_manager",
        description="Creates social posts",
        system_prompt="Create social posts",
        status=AgentStatus.ACTIVE,
        autonomy_level=1,
    )
    session.add_all([user, writer, keeper, smm])
    await session.flush()

    campaign = Campaign(
        name="Стратегия iTeam 2027",
        goal="Поддержать продажи стратегических сессий",
        status=CampaignStatus.ACTIVE,
        strategy_version=1,
        strategy={"recommended_article": {"title": "Статья кампании"}},
        created_by=user.id,
    )
    session.add(campaign)
    await session.flush()

    research_task = Task(
        campaign_id=campaign.id,
        task_type=TaskType.KNOWLEDGE_RESEARCH,
        title="Исследовать базу знаний",
        assigned_agent_id=keeper.id,
        status=TaskStatus.COMPLETED,
        priority=TaskPriority.NORMAL,
    )
    article_task = Task(
        campaign_id=campaign.id,
        task_type=TaskType.WRITE_ARTICLE,
        title="Написать статью",
        assigned_agent_id=writer.id,
        status=TaskStatus.COMPLETED,
        priority=TaskPriority.NORMAL,
    )
    session.add_all([research_task, article_task])
    await session.flush()

    research_run = AgentRun(
        agent_id=keeper.id,
        task_id=research_task.id,
        campaign_id=campaign.id,
        status=AgentRunStatus.COMPLETED,
        input_data={},
        output_data={},
        model="test-model",
        prompt_snapshot="",
        prompt_hash="test",
    )
    session.add(research_run)
    await session.flush()
    session.add(
        KnowledgePack(
            campaign_id=campaign.id,
            task_id=research_task.id,
            agent_run_id=research_run.id,
            created_by_agent_id=keeper.id,
            strategy_version=campaign.strategy_version,
            status=KnowledgePackStatus.READY,
            research_query="Тестовый запрос",
            summary="Готовый пакет знаний",
            gaps=[],
        )
    )

    article = ContentItem(
        campaign_id=campaign.id,
        source_task_id=article_task.id,
        content_type=ContentType.ARTICLE,
        title="Статья кампании",
        status=ContentStatus.APPROVED,
        author_agent_id=writer.id,
    )
    session.add(article)
    await session.flush()
    article_version = ContentVersion(
        content_item_id=article.id,
        version_number=2,
        content="Approved article body",
        structured_content={"body": "Approved article body"},
        created_by_agent_id=writer.id,
    )
    session.add(article_version)
    await session.flush()
    article.current_version_id = article_version.id
    session.add(
        Approval(
            object_type=ApprovalObjectType.CONTENT_ITEM,
            object_id=article.id,
            subject_version=2,
            status=ApprovalStatus.APPROVED,
            subject_snapshot={"content_version_id": str(article_version.id)},
        )
    )

    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=datetime.now(UTC),
        planning_horizon_end=datetime.now(UTC) + timedelta(days=14),
        timezone_policy="UTC",
        created_by_user_id=user.id,
        approved_at=datetime.now(UTC),
        approved_by_user_id=user.id,
    )
    session.add(plan)
    await session.flush()
    plan_item = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=1,
        scheduled_at=datetime.now(UTC) + timedelta(days=2),
        channel=ContentChannel.VK,
        source_content_item_id=article.id,
        source_content_version_id=article_version.id,
        topic="Тема из медиаплана",
        angle="Практический угол",
        purpose="Объяснить ценность",
        format="post",
        message_brief="Редакционный бриф",
        source_claim_ids=["article_test_p01"],
        source_support_summary="Подтверждённый тезис статьи.",
        status=PublicationPlanItemStatus.PLANNED,
    )
    session.add(plan_item)
    await session.flush()

    post: ContentItem | None = None
    post_version: ContentVersion | None = None
    publication: Publication | None = None
    if with_post:
        post_task = Task(
            campaign_id=campaign.id,
            task_type=TaskType.CREATE_SOCIAL_POSTS,
            title="Внутреннее название PlanItem",
            assigned_agent_id=smm.id,
            status=TaskStatus.COMPLETED,
            priority=TaskPriority.NORMAL,
            input_data={"publication_plan_item_id": str(plan_item.id)},
        )
        session.add(post_task)
        await session.flush()
        post = ContentItem(
            campaign_id=campaign.id,
            source_task_id=post_task.id,
            content_type=ContentType.SOCIAL_POST,
            title="Пост по статье",
            status=ContentStatus.APPROVED,
            author_agent_id=smm.id,
            metadata_={"publication_plan_item_id": str(plan_item.id)},
            channel=ContentChannel.VK,
        )
        session.add(post)
        await session.flush()
        post_version = ContentVersion(
            content_item_id=post.id,
            version_number=1,
            content="Post body",
            structured_content={"body": "Post body"},
            created_by_agent_id=smm.id,
        )
        session.add(post_version)
        await session.flush()
        post.current_version_id = post_version.id
        session.add(
            Approval(
                object_type=ApprovalObjectType.CONTENT_ITEM,
                object_id=post.id,
                subject_version=1,
                status=ApprovalStatus.APPROVED,
                subject_snapshot={
                    "posts": [
                        {
                            "content_item_id": str(post.id),
                            "content_version_id": str(post_version.id),
                        }
                    ]
                },
            )
        )
        publication = Publication(
            campaign_id=campaign.id,
            content_item_id=post.id,
            content_version_id=post_version.id,
            channel=ContentChannel.VK,
            status=PublicationStatus.SCHEDULED,
            scheduled_at=datetime.now(UTC) + timedelta(days=2),
        )
        session.add(publication)

        orphan_task = Task(
            campaign_id=campaign.id,
            task_type=TaskType.CREATE_SOCIAL_POSTS,
            title="Пост без plan item",
            assigned_agent_id=smm.id,
            status=TaskStatus.COMPLETED,
            priority=TaskPriority.NORMAL,
        )
        session.add(orphan_task)
        await session.flush()
        session.add(
            ContentItem(
                campaign_id=campaign.id,
                source_task_id=orphan_task.id,
                content_type=ContentType.SOCIAL_POST,
                title="Независимый пост",
                status=ContentStatus.APPROVED,
                author_agent_id=smm.id,
                channel=ContentChannel.VK,
            )
        )

    failure_task: Task | None = None
    if with_failure:
        failure_task = Task(
            campaign_id=campaign.id,
            task_type=TaskType.MANUAL,
            title="Техническое старое название",
            assigned_agent_id=smm.id,
            status=TaskStatus.FAILED,
            priority=TaskPriority.NORMAL,
            retry_count=1,
        )
        session.add(failure_task)
        await session.flush()
        session.add(
            AgentRun(
                agent_id=smm.id,
                task_id=failure_task.id,
                campaign_id=campaign.id,
                status=AgentRunStatus.FAILED,
                input_data={},
                output_data={},
                model="test-model",
                prompt_snapshot="",
                prompt_hash="test",
                error_code="WORKER_INTERRUPTED",
                error_message="raw technical text must not be included in the workspace summary",
            )
        )

    await session.commit()
    return {
        "campaign": campaign,
        "article": article,
        "article_version": article_version,
        "plan": plan,
        "plan_item": plan_item,
        "post": post,
        "post_version": post_version,
        "publication": publication,
        "failure_task": failure_task,
    }


async def test_workspace_exposes_only_persisted_article_plan_post_and_publication_links(
    db_session: AsyncSession,
) -> None:
    fixture = await workspace_fixture(db_session)
    campaign = fixture["campaign"]
    article = fixture["article"]
    article_version = fixture["article_version"]
    plan = fixture["plan"]
    plan_item = fixture["plan_item"]
    post = fixture["post"]
    publication = fixture["publication"]

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)

    assert workspace.director.plan_status is PublicationPlanStatus.APPROVED
    assert workspace.director.plan_item_count == 1
    assert workspace.director.plan_items_with_posts == 1
    assert workspace.director.plan_items_without_posts == 0
    assert len(workspace.social_posts) == 2
    article_card = next(row for row in workspace.articles if row.id == article.id)
    assert article_card.approved_version_id == article_version.id
    assert article_card.approved_version_number == 2
    assert article_card.plan_item_count == 1
    assert article_card.social_post_count == 1
    assert article_card.scheduled_publication_count == 1
    assert workspace.publication_plan is not None
    assert workspace.publication_plan.id == plan.id
    item = workspace.publication_plan.items[0]
    assert item.id == plan_item.id
    assert item.source_content_item_id == article.id
    assert item.source_content_version_id == article_version.id
    assert item.source_version_number == 2
    assert [row.id for row in item.social_posts] == [post.id]
    assert item.social_posts[0].approved_version_id == fixture["post_version"].id
    assert [row.id for row in item.publications] == [publication.id]
    assert item.publications[0].publication_plan_item_id == plan_item.id
    assert [stage.label for stage in item.pipeline] == [
        "Статья",
        "Пост",
        "Согласование",
        "Запланировано",
        "Опубликовано",
    ]


async def test_workspace_does_not_claim_a_version_from_another_article_as_source(
    db_session: AsyncSession,
) -> None:
    fixture = await workspace_fixture(db_session, with_post=False)
    campaign = fixture["campaign"]
    article = fixture["article"]
    plan_item = fixture["plan_item"]

    unrelated_article = ContentItem(
        campaign_id=campaign.id,
        source_task_id=article.source_task_id,
        content_type=ContentType.ARTICLE,
        title="Другая статья",
        status=ContentStatus.APPROVED,
        author_agent_id=article.author_agent_id,
    )
    db_session.add(unrelated_article)
    await db_session.flush()
    unrelated_version = ContentVersion(
        content_item_id=unrelated_article.id,
        version_number=7,
        content="Unrelated article body",
        structured_content={"body": "Unrelated article body"},
        created_by_agent_id=article.author_agent_id,
    )
    db_session.add(unrelated_version)
    await db_session.flush()
    plan_item.source_content_version_id = unrelated_version.id
    plan_item.source_claim_ids = ["claim-from-unrelated-version"]
    plan_item.source_support_summary = "Основание другой версии"
    await db_session.commit()

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)
    assert workspace.publication_plan is not None
    item = workspace.publication_plan.items[0]

    assert item.source_content_item_title == article.title
    assert item.source_content_version_id is None
    assert item.source_version_number is None
    assert item.source_claim_ids is None
    assert item.source_support_summary is None
    assert item.pipeline[0].status == "unverified"
    assert item.pipeline[0].action_label == "Версия источника не подтверждена"
    assert item.pipeline[1].action_label is None
    assert item.post_action.allowed is False
    assert item.post_action.error_code in {
        "PUBLICATION_PLAN_SOURCE_INVALID",
        "PUBLICATION_PLAN_SOURCE_NOT_APPROVED",
    }
    assert workspace.director.next_step.entity_id == plan_item.id
    assert workspace.director.next_step.title == "Проверить условия создания поста"


async def test_director_prioritizes_failed_task_and_uses_safe_explanation(
    db_session: AsyncSession,
) -> None:
    fixture = await workspace_fixture(db_session, with_post=False, with_failure=True)
    campaign = fixture["campaign"]
    failure_task = fixture["failure_task"]

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)

    assert workspace.director.failed_task_count == 1
    assert workspace.director.next_step.entity_type == "task"
    assert workspace.director.next_step.entity_id == failure_task.id
    task_card = next(row for row in workspace.attention_tasks if row.id == failure_task.id)
    assert task_card.error_summary == "Выполнение остановилось до завершения."
    assert task_card.next_action == "Откройте задачу, проверьте состояние и доступность повтора."
    assert task_card.retry_allowed is True
    assert "raw technical text" not in str(workspace.model_dump())


async def test_director_ignores_failed_plan_item_task_after_post_exists(
    db_session: AsyncSession,
) -> None:
    fixture = await workspace_fixture(db_session, with_post=True)
    campaign = fixture["campaign"]
    plan = fixture["plan"]
    completed_item = fixture["plan_item"]
    article = fixture["article"]
    article_version = fixture["article_version"]
    stale_failure = Task(
        campaign_id=campaign.id,
        task_type=TaskType.CREATE_SOCIAL_POSTS,
        title="Старая ошибка для уже созданного поста",
        status=TaskStatus.FAILED,
        priority=TaskPriority.NORMAL,
        input_data={"publication_plan_item_id": str(completed_item.id)},
    )
    next_item = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=2,
        scheduled_at=datetime.now(UTC) + timedelta(days=3),
        channel=ContentChannel.TELEGRAM,
        source_content_item_id=article.id,
        source_content_version_id=article_version.id,
        topic="Следующий актуальный пост",
        angle="Практический угол",
        purpose="Продолжить кампанию",
        format="post",
        message_brief="Редакционный бриф",
        source_claim_ids=["article_test_p01"],
        source_support_summary="Подтверждённый тезис статьи.",
        status=PublicationPlanItemStatus.PLANNED,
    )
    db_session.add_all([stale_failure, next_item])
    await db_session.commit()

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)

    assert workspace.director.failed_task_count == 1
    assert workspace.director.next_step.entity_type == "publication_plan_item"
    assert workspace.director.next_step.entity_id == next_item.id
    assert workspace.director.next_step.title == "Создать TELEGRAM-пост для пункта №2"


async def test_director_prioritizes_pending_post_approval_before_next_missing_post(
    db_session: AsyncSession,
) -> None:
    fixture = await workspace_fixture(db_session, with_post=True)
    campaign = fixture["campaign"]
    plan = fixture["plan"]
    pending_post = fixture["post"]
    article = fixture["article"]
    article_version = fixture["article_version"]
    pending_post.status = ContentStatus.WAITING_APPROVAL
    next_item = PublicationPlanItem(
        publication_plan_id=plan.id,
        position=2,
        scheduled_at=datetime.now(UTC) + timedelta(days=3),
        channel=ContentChannel.TELEGRAM,
        source_content_item_id=article.id,
        source_content_version_id=article_version.id,
        topic="Следующий актуальный пост",
        angle="Практический угол",
        purpose="Продолжить кампанию",
        format="post",
        message_brief="Редакционный бриф",
        source_claim_ids=["article_test_p01"],
        source_support_summary="Подтверждённый тезис статьи.",
        status=PublicationPlanItemStatus.PLANNED,
    )
    db_session.add(next_item)
    await db_session.commit()

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)

    assert workspace.director.next_step.entity_type == "social_post"
    assert workspace.director.next_step.entity_id == pending_post.id
    assert workspace.director.next_step.title == "Согласовать пост"
    assert workspace.director.next_step.href == f"/content/{pending_post.id}#approval"


async def test_director_links_to_the_first_missing_social_post_for_approved_plan(
    db_session: AsyncSession,
) -> None:
    fixture = await workspace_fixture(db_session, with_post=False)
    campaign = fixture["campaign"]
    plan_item = fixture["plan_item"]

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)

    assert workspace.publication_plan is not None
    action = workspace.publication_plan.items[0].post_action
    assert action.allowed is True
    assert action.error_code is None
    assert workspace.director.next_step.entity_type == "publication_plan_item"
    assert workspace.director.next_step.entity_id == plan_item.id
    assert workspace.director.next_step.title == "Создать VK-пост для пункта №1"
    assert workspace.director.next_step.href == (
        f"/campaigns/{campaign.id}#plan-item-{plan_item.id}"
    )
