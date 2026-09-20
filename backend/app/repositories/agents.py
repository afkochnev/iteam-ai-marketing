from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent, AgentTool


class AgentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_agents(self) -> list[Agent]:
        position = case(
            (Agent.slug == "marketing_director", 1),
            (Agent.slug == "knowledge_keeper", 2),
            (Agent.slug == "writer", 3),
            (Agent.slug == "smm_manager", 4),
            else_=99,
        )
        result = await self.session.execute(select(Agent).order_by(position, Agent.name))
        return list(result.scalars().all())

    async def get_by_id(self, agent_id: UUID, *, with_tools: bool = False) -> Agent | None:
        statement = select(Agent).where(Agent.id == agent_id)
        if with_tools:
            statement = statement.options(selectinload(Agent.tools))
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str, *, with_tools: bool = False) -> Agent | None:
        statement = select(Agent).where(Agent.slug == slug)
        if with_tools:
            statement = statement.options(selectinload(Agent.tools))
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def update_agent(self, agent: Agent, changes: Mapping[str, Any]) -> Agent:
        for field, value in changes.items():
            setattr(agent, field, value)
        await self.session.flush()
        await self.session.refresh(agent)
        return agent

    async def list_tools(self, agent_id: UUID) -> list[AgentTool]:
        result = await self.session.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id).order_by(AgentTool.tool_name)
        )
        return list(result.scalars().all())

    async def get_tool(self, agent_id: UUID, tool_id: UUID) -> AgentTool | None:
        result = await self.session.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.id == tool_id)
        )
        return result.scalar_one_or_none()

    async def update_tool(self, tool: AgentTool, changes: Mapping[str, Any]) -> AgentTool:
        for field, value in changes.items():
            setattr(tool, field, value)
        await self.session.flush()
        await self.session.refresh(tool)
        return tool
