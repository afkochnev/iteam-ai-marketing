from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.agent import Agent, AgentRole, AgentSlug, AgentStatus, AgentTool
from app.models.user import UserRole
from app.repositories.agents import AgentRepository
from app.repositories.users import UserRepository
from app.seed import AGENT_SEEDS, seed_agents


async def create_agent(session: AsyncSession, slug: str = AgentSlug.WRITER) -> Agent:
    agent = Agent(
        name="Writer",
        slug=slug,
        role=AgentRole.CONTENT_WRITER,
        description="Test writer",
        system_prompt="Write only grounded content.",
        status=AgentStatus.ACTIVE,
        autonomy_level=2,
        settings={"language": "ru"},
    )
    session.add(agent)
    await session.flush()
    return agent


async def create_user_and_login(client: AsyncClient, session: AsyncSession, role: UserRole) -> None:
    email = f"{role.value.lower()}-{uuid4()}@example.com"
    await UserRepository(session).create(
        email=email,
        password_hash=hash_password("valid-password"),
        full_name="API User",
        role=role,
    )
    await session.commit()
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "valid-password"},
    )
    assert response.status_code == 200


async def test_agent_and_tool_model_defaults_and_cascade(db_session: AsyncSession) -> None:
    agent = await create_agent(db_session)
    tool = AgentTool(agent_id=agent.id, tool_name="read_campaign")
    db_session.add(tool)
    await db_session.commit()
    await db_session.refresh(agent)
    await db_session.refresh(tool)

    assert agent.id is not None
    assert agent.status is AgentStatus.ACTIVE
    assert agent.autonomy_level == 2
    assert agent.settings == {"language": "ru"}
    assert agent.created_at.tzinfo is not None
    assert tool.is_enabled is True
    assert tool.requires_approval is False
    tool_id = tool.id

    duplicate = AgentTool(agent_id=agent.id, tool_name="read_campaign")
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    await db_session.delete(agent)
    await db_session.commit()
    assert await db_session.get(AgentTool, tool_id) is None


async def test_seed_is_idempotent_and_preserves_changes(db_session: AsyncSession) -> None:
    assert await seed_agents() == (4, 14)
    repository = AgentRepository(db_session)
    writer = await repository.get_by_slug(AgentSlug.WRITER, with_tools=True)
    assert writer is not None
    writer.system_prompt = "Manually edited prompt"
    writer.model = "custom-model"
    revision_tool = next(
        tool for tool in writer.tools if tool.tool_name == "create_content_revision"
    )
    revision_tool.is_enabled = False
    await db_session.commit()

    await db_session.bind.dispose()  # type: ignore[union-attr]
    assert await seed_agents() == (0, 0)
    await db_session.bind.dispose()  # type: ignore[union-attr]
    preserved = await repository.get_by_slug(AgentSlug.WRITER, with_tools=True)
    assert preserved is not None
    assert preserved.system_prompt == "Manually edited prompt"
    assert preserved.model == "custom-model"
    assert (
        next(t for t in preserved.tools if t.tool_name == "create_content_revision").is_enabled
        is False
    )
    assert len(await repository.list_agents()) == len(AGENT_SEEDS)


async def test_agents_api_rbac_and_validation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_agents()
    await db_session.bind.dispose()  # type: ignore[union-attr]
    assert (await client.get("/api/v1/agents")).status_code == 401

    await create_user_and_login(client, db_session, UserRole.MANAGER)
    response = await client.get("/api/v1/agents")
    assert response.status_code == 200
    assert len(response.json()) == 4
    writer_id = next(item["id"] for item in response.json() if item["slug"] == "writer")
    details = await client.get(f"/api/v1/agents/{writer_id}")
    assert details.status_code == 200
    assert len(details.json()["tools"]) == 4
    assert (
        await client.patch(f"/api/v1/agents/{writer_id}", json={"description": "No"})
    ).status_code == 403

    await client.post("/api/v1/auth/logout")
    await create_user_and_login(client, db_session, UserRole.ADMIN)
    updated = await client.patch(
        f"/api/v1/agents/{writer_id}",
        json={"description": "Updated", "system_prompt": "Updated prompt", "model": "  "},
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "Updated"
    assert updated.json()["model"] is None
    invalid = await client.patch(f"/api/v1/agents/{writer_id}", json={"autonomy_level": 6})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await client.get(f"/api/v1/agents/{uuid4()}")).status_code == 404


async def test_tool_update_rbac(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_agents()
    await db_session.bind.dispose()  # type: ignore[union-attr]
    await create_user_and_login(client, db_session, UserRole.ADMIN)
    agents = (await client.get("/api/v1/agents")).json()
    writer_id = next(item["id"] for item in agents if item["slug"] == "writer")
    details = (await client.get(f"/api/v1/agents/{writer_id}")).json()
    tool_id = details["tools"][0]["id"]
    response = await client.patch(
        f"/api/v1/agents/{writer_id}/tools/{tool_id}",
        json={"is_enabled": False, "requires_approval": True},
    )
    assert response.status_code == 200
    assert response.json()["is_enabled"] is False
    assert response.json()["requires_approval"] is True
    assert (
        await client.patch(f"/api/v1/agents/{writer_id}/tools/{uuid4()}", json={"is_enabled": True})
    ).status_code == 404

    await client.post("/api/v1/auth/logout")
    await create_user_and_login(client, db_session, UserRole.MANAGER)
    assert (
        await client.patch(f"/api/v1/agents/{writer_id}/tools/{tool_id}", json={"is_enabled": True})
    ).status_code == 403
