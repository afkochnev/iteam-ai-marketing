from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import AdminUser, CurrentUser, SessionDependency
from app.schemas.agent import (
    AgentListItem,
    AgentResponse,
    AgentToolResponse,
    AgentToolUpdate,
    AgentUpdate,
)
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=list[AgentListItem])
async def list_agents(
    _current_user: CurrentUser, session: SessionDependency
) -> list[AgentListItem]:
    return [
        AgentListItem.model_validate(agent) for agent in await AgentService(session).list_agents()
    ]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID, _current_user: CurrentUser, session: SessionDependency
) -> AgentResponse:
    return AgentResponse.model_validate(await AgentService(session).get_agent(agent_id))


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    payload: AgentUpdate,
    session: SessionDependency,
    _admin: AdminUser,
) -> AgentResponse:
    return AgentResponse.model_validate(await AgentService(session).update_agent(agent_id, payload))


@router.patch("/{agent_id}/tools/{tool_id}", response_model=AgentToolResponse)
async def update_agent_tool(
    agent_id: UUID,
    tool_id: UUID,
    payload: AgentToolUpdate,
    session: SessionDependency,
    _admin: AdminUser,
) -> AgentToolResponse:
    return AgentToolResponse.model_validate(
        await AgentService(session).update_tool(agent_id, tool_id, payload)
    )
