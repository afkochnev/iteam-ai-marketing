from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.activity import ActivityLog
from app.models.agent import Agent, AgentStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import CampaignStatus
from app.models.content import (
    ContentChannel,
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
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.schemas.campaign import CampaignCreate
from app.services.campaign_service import CampaignService
from app.services.campaign_workspace_service import CampaignWorkspaceService

STRATEGY_V1 = {
    "campaign_summary": "Стратегия v1",
    "positioning": "Управляемый рост",
    "target_audience": "CEO среднего бизнеса",
    "main_message": "Верните управляемость",
    "content_strategy": "Экспертные материалы",
    "content_topics": ["Рост", "Управление", "Команда"],
    "recommended_article": {
        "title": "Статья",
        "objective": "Объяснить проблему",
        "angle": "Практика",
        "cta": "Обсудить",
    },
    "social_strategy": {
        "channels": ["TELEGRAM"],
        "post_count": 3,
        "approach": "Последовательно раскрыть тему",
    },
    "tasks": [],
}


async def _login(client: AsyncClient, user: User) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "password"}
    )
    assert response.status_code == 200


async def _active_campaign(session: AsyncSession) -> tuple[User, object]:
    user = await UserRepository(session).create(
        email=f"change-{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Campaign owner",
        role=UserRole.ADMIN,
    )
    campaign = await CampaignService(session).create_campaign(
        CampaignCreate(
            name="Стратегия iTeam",
            description="Исходное описание",
            goal="Получить заявки",
            product="Диагностика",
            target_audience="CEO среднего бизнеса",
            offer="Стратегическая сессия",
            desired_result="30 заявок",
        ),
        user,
    )
    campaign.status = CampaignStatus.ACTIVE
    campaign.strategy = STRATEGY_V1
    campaign.strategy_version = 1
    session.add(
        Approval(
            object_type=ApprovalObjectType.CAMPAIGN_STRATEGY,
            object_id=campaign.id,
            subject_version=1,
            status=ApprovalStatus.APPROVED,
            reviewed_by_user_id=user.id,
            subject_snapshot=STRATEGY_V1,
            metadata_={},
            resolved_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(campaign)
    return user, campaign


async def _downstream_fixture(session: AsyncSession, campaign, user: User) -> dict[str, object]:
    agent = Agent(
        name="Fixture agent",
        slug=f"fixture-{uuid4()}",
        role="fixture",
        system_prompt="fixture",
        model="fixture",
        status=AgentStatus.ACTIVE,
        autonomy_level=0,
        settings={},
    )
    session.add(agent)
    await session.flush()
    task = Task(
        campaign_id=campaign.id,
        task_type=TaskType.MANUAL,
        title="Fixture",
        assigned_agent_id=agent.id,
        priority=TaskPriority.NORMAL,
        status=TaskStatus.COMPLETED,
        input_data={"strategy_version": 1},
        output_data={},
    )
    session.add(task)
    await session.flush()

    async def content(title: str, content_type: ContentType, channel=None):
        item = ContentItem(
            campaign_id=campaign.id,
            source_task_id=task.id,
            content_type=content_type,
            title=title,
            status=ContentStatus.APPROVED,
            author_agent_id=agent.id,
            metadata_={},
            channel=channel,
        )
        session.add(item)
        await session.flush()
        version = ContentVersion(
            content_item_id=item.id,
            version_number=1,
            content=f"Текст: {title}",
            structured_content={},
            created_by_agent_id=agent.id,
            change_description="fixture",
        )
        session.add(version)
        await session.flush()
        item.current_version_id = version.id
        session.add(
            Approval(
                object_type=ApprovalObjectType.CONTENT_ITEM,
                object_id=item.id,
                subject_version=1,
                status=ApprovalStatus.APPROVED,
                reviewed_by_user_id=user.id,
                subject_snapshot={"content_version_id": str(version.id)},
                metadata_={},
                resolved_at=datetime.now(UTC),
            )
        )
        return item, version

    article, article_version = await content("Статья", ContentType.ARTICLE)
    post_scheduled, scheduled_version = await content(
        "Запланированный пост", ContentType.SOCIAL_POST, ContentChannel.TELEGRAM
    )
    post_published, published_version = await content(
        "Опубликованный пост", ContentType.SOCIAL_POST, ContentChannel.TELEGRAM
    )
    plan = PublicationPlan(
        campaign_id=campaign.id,
        status=PublicationPlanStatus.APPROVED,
        planning_horizon_start=datetime.now(UTC),
        planning_horizon_end=datetime.now(UTC) + timedelta(days=30),
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
        scheduled_at=datetime.now(UTC) + timedelta(days=7),
        channel=ContentChannel.TELEGRAM,
        source_content_item_id=article.id,
        source_content_version_id=article_version.id,
        topic="Тема",
        angle="Ракурс",
        purpose="Цель",
        format="post",
        message_brief="Brief",
        status=PublicationPlanItemStatus.PLANNED,
    )
    session.add(plan_item)
    scheduled_at = datetime.now(UTC) + timedelta(days=5)
    scheduled = Publication(
        campaign_id=campaign.id,
        content_item_id=post_scheduled.id,
        content_version_id=scheduled_version.id,
        channel=ContentChannel.TELEGRAM,
        status=PublicationStatus.SCHEDULED,
        scheduled_at=scheduled_at,
        approved_for_publish_at=datetime.now(UTC),
        approved_for_publish_by=user.id,
    )
    published_at = datetime.now(UTC) - timedelta(days=1)
    published = Publication(
        campaign_id=campaign.id,
        content_item_id=post_published.id,
        content_version_id=published_version.id,
        channel=ContentChannel.TELEGRAM,
        status=PublicationStatus.PUBLISHED,
        published_at=published_at,
        external_id="history-1",
    )
    session.add_all([scheduled, published])
    await session.commit()
    return {
        "plan": plan,
        "plan_item": plan_item,
        "article": article,
        "article_version": article_version,
        "scheduled": scheduled,
        "scheduled_at": scheduled_at,
        "published": published,
        "published_at": published_at,
    }


async def test_administrative_change_is_audited_without_strategy_revision(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, campaign = await _active_campaign(db_session)
    await _login(client, user)
    preview = await client.post(
        f"/api/v1/campaigns/{campaign.id}/changes/preview",
        json={"changes": {"name": "Новое название"}},
    )
    assert preview.status_code == 200
    assert preview.json()["kind"] == "ADMINISTRATIVE"
    assert preview.json()["requires_confirmation"] is False

    applied = await client.post(
        f"/api/v1/campaigns/{campaign.id}/changes",
        json={
            "changes": {"name": "Новое название"},
            "confirmed_impact": False,
            "expected_updated_at": campaign.updated_at.isoformat(),
        },
    )
    assert applied.status_code == 200
    assert applied.json()["campaign"]["name"] == "Новое название"
    assert applied.json()["campaign"]["strategy_version"] == 1
    assert applied.json()["campaign"]["strategy"] == STRATEGY_V1
    event = await db_session.scalar(
        select(ActivityLog).where(ActivityLog.event_type == "CAMPAIGN_ADMINISTRATIVE_UPDATED")
    )
    assert event is not None
    assert event.metadata_["changes"][0] == {
        "field": "name",
        "label": "Название",
        "old_value": "Стратегия iTeam",
        "new_value": "Новое название",
        "kind": "ADMINISTRATIVE",
    }


async def test_strategic_change_requires_preview_and_preserves_downstream(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, campaign = await _active_campaign(db_session)
    fixture = await _downstream_fixture(db_session, campaign, user)
    await db_session.refresh(campaign)
    await _login(client, user)

    blind = await client.patch(
        f"/api/v1/campaigns/{campaign.id}",
        json={"target_audience": "Собственники компаний 50–500 сотрудников"},
    )
    assert blind.status_code == 409
    assert blind.json()["error"]["code"] == "CAMPAIGN_STRATEGIC_CHANGE_REQUIRES_PREVIEW"

    preview = await client.post(
        f"/api/v1/campaigns/{campaign.id}/changes/preview",
        json={"changes": {"target_audience": "Собственники компаний 50–500 сотрудников"}},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["kind"] == "STRATEGIC"
    assert body["requires_confirmation"] is True
    assert body["impact"]["strategy_version"] == 1
    assert body["impact"]["publication_plan_status"] == "APPROVED"
    assert body["impact"]["future_plan_item_count"] == 1
    assert body["impact"]["approved_social_post_count"] == 2
    assert body["impact"]["scheduled_publication_count"] == 1
    assert body["impact"]["published_publication_count"] == 1

    unconfirmed = await client.post(
        f"/api/v1/campaigns/{campaign.id}/changes",
        json={
            "changes": {"target_audience": "Собственники компаний 50–500 сотрудников"},
            "confirmed_impact": False,
            "expected_updated_at": campaign.updated_at.isoformat(),
        },
    )
    assert unconfirmed.status_code == 409

    applied = await client.post(
        f"/api/v1/campaigns/{campaign.id}/changes",
        json={
            "changes": {"target_audience": "Собственники компаний 50–500 сотрудников"},
            "comment": "Уточнили сегмент",
            "confirmed_impact": True,
            "expected_updated_at": campaign.updated_at.isoformat(),
        },
    )
    assert applied.status_code == 200
    current = await CampaignService(db_session).get_campaign(campaign.id)
    assert current.target_audience == "Собственники компаний 50–500 сотрудников"
    assert current.strategy == STRATEGY_V1
    assert current.strategy_version == 1
    assert fixture["article_version"].content == "Текст: Статья"
    assert fixture["plan"].status is PublicationPlanStatus.APPROVED
    assert fixture["plan_item"].topic == "Тема"
    assert fixture["scheduled"].status is PublicationStatus.SCHEDULED
    assert fixture["scheduled"].scheduled_at == fixture["scheduled_at"]
    assert fixture["published"].status is PublicationStatus.PUBLISHED
    assert fixture["published"].published_at == fixture["published_at"]

    workspace = await CampaignWorkspaceService(db_session).get(campaign.id)
    assert workspace.change_state.has_pending_strategic_changes is True
    assert workspace.change_state.plan_requires_review is True
    assert workspace.director.next_step.title == "Обновить стратегию кампании"
    assert workspace.director.next_step.priority == "HIGH"
    audit = await db_session.scalar(
        select(ActivityLog).where(ActivityLog.event_type == "CAMPAIGN_STRATEGIC_CHANGED")
    )
    assert audit is not None
    assert audit.metadata_["strategy_version"] == 1
    assert audit.metadata_["comment"] == "Уточнили сегмент"

    fixture["plan"].created_at = audit.created_at + timedelta(seconds=1)
    current.strategy_version = 2
    current.strategy = {
        **STRATEGY_V1,
        "target_audience": "Собственники компаний 50–500 сотрудников",
    }
    current.status = CampaignStatus.ACTIVE
    db_session.add(
        Approval(
            object_type=ApprovalObjectType.CAMPAIGN_STRATEGY,
            object_id=current.id,
            subject_version=2,
            status=ApprovalStatus.APPROVED,
            reviewed_by_user_id=user.id,
            subject_snapshot=current.strategy,
            metadata_={},
            resolved_at=audit.created_at + timedelta(seconds=2),
        )
    )
    await db_session.commit()
    after_approval = await CampaignWorkspaceService(db_session).get(campaign.id)
    assert after_approval.change_state.has_pending_strategic_changes is False
    assert after_approval.change_state.plan_requires_review is True
    assert after_approval.director.next_step.title == "Пересмотреть медиаплан"
