import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent import Agent, AgentTool
from app.repositories.agents import AgentRepository
from app.schemas.agent import AgentToolUpdate, AgentUpdate

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = AgentRepository(session)

    async def list_agents(self) -> list[Agent]:
        return await self.repository.list_agents()

    async def get_agent(self, agent_id: UUID) -> Agent:
        agent = await self.repository.get_by_id(agent_id, with_tools=True)
        if agent is None:
            raise AppError("AGENT_NOT_FOUND", "Агент не найден.", 404)
        return agent

    async def update_agent(self, agent_id: UUID, payload: AgentUpdate) -> Agent:
        agent = await self.repository.get_by_id(agent_id)
        if agent is None:
            raise AppError("AGENT_NOT_FOUND", "Агент не найден.", 404)
        await self.repository.update_agent(agent, payload.model_dump(exclude_unset=True))
        await self.session.commit()
        logger.info("Agent updated", extra={"agent_id": str(agent.id)})
        return await self.get_agent(agent.id)

    async def update_tool(
        self, agent_id: UUID, tool_id: UUID, payload: AgentToolUpdate
    ) -> AgentTool:
        if await self.repository.get_by_id(agent_id) is None:
            raise AppError("AGENT_NOT_FOUND", "Агент не найден.", 404)
        tool = await self.repository.get_tool(agent_id, tool_id)
        if tool is None:
            raise AppError("AGENT_TOOL_NOT_FOUND", "Инструмент агента не найден.", 404)
        await self.repository.update_tool(tool, payload.model_dump(exclude_unset=True))
        await self.session.commit()
        logger.info(
            "Agent tool updated", extra={"agent_id": str(agent_id), "agent_tool_id": str(tool.id)}
        )
        return tool
