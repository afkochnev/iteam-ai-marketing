from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import hash_password
from app.models.agent import Agent, AgentStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.task import TaskPriority, TaskStatus, TaskType
from app.models.user import User, UserRole
from app.repositories.users import UserRepository
from app.schemas.campaign import CampaignCreate
from app.schemas.task import TaskCompleteRequest, TaskCreate, TaskUpdate
from app.services.campaign_service import CampaignService
from app.services.task_service import TaskService


async def user(session: AsyncSession, role: UserRole = UserRole.ADMIN) -> User:
    result = await UserRepository(session).create(
        email=f"{uuid4()}@example.com",
        password_hash=hash_password("password"),
        full_name="Tester",
        role=role,
    )
    await session.commit()
    return result


async def campaign(session: AsyncSession, owner: User, name: str = "Campaign") -> Campaign:
    return await CampaignService(session).create_campaign(
        CampaignCreate(name=name, goal="Goal"), owner
    )


async def agent(session: AsyncSession, slug: str = "test-agent") -> Agent:
    result = Agent(
        name="Agent",
        slug=f"{slug}-{uuid4()}",
        role="test",
        system_prompt="Prompt",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={},
    )
    session.add(result)
    await session.commit()
    return result


def payload(
    campaign_id: UUID,
    title: str,
    dependencies: list[UUID] | None = None,
    assigned_agent_id: UUID | None = None,
) -> TaskCreate:
    return TaskCreate(
        campaign_id=campaign_id,
        title=title,
        task_type=TaskType.MANUAL,
        dependency_ids=dependencies or [],
        assigned_agent_id=assigned_agent_id,
    )


def test_task_schemas_reject_protected_fields_and_empty_title() -> None:
    with pytest.raises(ValidationError):
        TaskCreate.model_validate({"campaign_id": str(uuid4()), "title": " ", "status": "READY"})
    with pytest.raises(ValidationError):
        TaskUpdate.model_validate({"output_data": {"fake": True}})


async def test_creation_status_relations_and_json(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    current_campaign = await campaign(db_session, owner)
    current_agent = await agent(db_session)
    service = TaskService(db_session)
    first = await service.create_task(
        payload(current_campaign.id, "First", assigned_agent_id=current_agent.id)
    )
    blocked = await service.create_task(payload(current_campaign.id, "Blocked", [first.id]))
    assert first.status is TaskStatus.READY
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.dependencies[0].depends_on_task.id == first.id
    assert first.priority is TaskPriority.NORMAL
    assert first.input_data == first.output_data == {}
    assert first.created_at.tzinfo is not None


async def test_multi_dependency_unblocks_atomically(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    current_campaign = await campaign(db_session, owner)
    service = TaskService(db_session)
    first = await service.create_task(payload(current_campaign.id, "A"))
    second = await service.create_task(payload(current_campaign.id, "B"))
    downstream = await service.create_task(payload(current_campaign.id, "C", [first.id, second.id]))
    for upstream in (first, second):
        started = await service.start_task(upstream.id)
        assert started.started_at is not None
        completed = await service.complete_task(upstream.id, {"result": upstream.title})
        assert completed.completed_at is not None
        downstream = await service.get_task(downstream.id)
        assert downstream.status is (
            TaskStatus.BLOCKED if upstream.id == first.id else TaskStatus.READY
        )
        if upstream.id == first.id:
            already_completed = await service.create_task(
                payload(current_campaign.id, "Completed dependency", [first.id])
            )
            assert already_completed.status is TaskStatus.READY


async def test_dependency_guards_and_remove(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    first_campaign = await campaign(db_session, owner, "One")
    second_campaign = await campaign(db_session, owner, "Two")
    service = TaskService(db_session)
    first = await service.create_task(payload(first_campaign.id, "A"))
    second = await service.create_task(payload(first_campaign.id, "B", [first.id]))
    third = await service.create_task(payload(first_campaign.id, "C", [second.id]))
    other = await service.create_task(payload(second_campaign.id, "Other"))
    for dependency_id, code in (
        (second.id, "TASK_DEPENDENCY_CYCLE"),
        (first.id, "TASK_SELF_DEPENDENCY"),
        (other.id, "TASK_CROSS_CAMPAIGN_DEPENDENCY"),
    ):
        with pytest.raises(AppError) as error:
            await service.add_dependency(first.id, dependency_id)
        assert error.value.code == code
    with pytest.raises(AppError) as duplicate:
        await service.add_dependency(second.id, first.id)
    assert duplicate.value.code == "TASK_DEPENDENCY_ALREADY_EXISTS"
    with pytest.raises(AppError) as longer_cycle:
        await service.add_dependency(first.id, third.id)
    assert longer_cycle.value.code == "TASK_DEPENDENCY_CYCLE"
    removed = await service.remove_dependency(second.id, first.id)
    assert removed.status is TaskStatus.READY


async def test_lifecycle_cancel_and_archive_guards(db_session: AsyncSession) -> None:
    owner = await user(db_session)
    current_campaign = await campaign(db_session, owner)
    service = TaskService(db_session)
    upstream = await service.create_task(payload(current_campaign.id, "A"))
    blocked = await service.create_task(payload(current_campaign.id, "B", [upstream.id]))
    with pytest.raises(AppError):
        await service.start_task(blocked.id)
    with pytest.raises(AppError):
        await service.complete_task(upstream.id, {})
    cancelled = await service.cancel_task(upstream.id)
    assert cancelled.status is TaskStatus.CANCELLED
    with pytest.raises(AppError):
        await service.start_task(cancelled.id)
    current_campaign.status = CampaignStatus.ARCHIVED
    await db_session.commit()
    with pytest.raises(AppError) as archived:
        await service.create_task(payload(current_campaign.id, "No"))
    assert archived.value.code == "CAMPAIGN_ARCHIVED"
    with pytest.raises(AppError):
        await service.update_task(blocked.id, TaskUpdate(title="No"))


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.MANAGER])
async def test_task_api_workflow_and_filters(
    client: AsyncClient, db_session: AsyncSession, role: UserRole
) -> None:
    owner = await user(db_session, role)
    current_campaign = await campaign(db_session, owner)
    current_agent = await agent(db_session)
    assert (await client.get("/api/v1/tasks")).status_code == 401
    assert (
        await client.post("/api/v1/auth/login", json={"email": owner.email, "password": "password"})
    ).status_code == 200
    first = await client.post(
        "/api/v1/tasks",
        json={
            "campaign_id": str(current_campaign.id),
            "title": "A",
            "task_type": "KNOWLEDGE_RESEARCH",
            "assigned_agent_id": str(current_agent.id),
        },
    )
    assert first.status_code == 201
    first_id = first.json()["id"]
    second = await client.post(
        "/api/v1/tasks",
        json={"campaign_id": str(current_campaign.id), "title": "B", "dependency_ids": [first_id]},
    )
    assert second.status_code == 201 and second.json()["status"] == "BLOCKED"
    assert (
        len(
            (
                await client.get(
                    f"/api/v1/tasks?campaign_id={current_campaign.id}&status=READY&priority=NORMAL"
                )
            ).json()
        )
        == 1
    )
    assert (
        await client.patch(f"/api/v1/tasks/{first_id}", json={"status": "COMPLETED"})
    ).status_code == 422
    started = await client.post(f"/api/v1/tasks/{first_id}/start")
    assert started.status_code == 200 and started.json()["started_at"]
    completed = await client.post(
        f"/api/v1/tasks/{first_id}/complete",
        json=TaskCompleteRequest(output_data={"ok": True}).model_dump(),
    )
    assert completed.status_code == 200
    assert (await client.get(f"/api/v1/tasks/{second.json()['id']}")).json()["status"] == "READY"
    assert (await client.post(f"/api/v1/tasks/{second.json()['id']}/cancel")).json()[
        "status"
    ] == "CANCELLED"
    assert (await client.get(f"/api/v1/tasks/{uuid4()}")).status_code == 404
