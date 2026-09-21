from uuid import UUID

from fastapi import APIRouter

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.agent_run import AgentRunStatus
from app.models.user import UserRole
from app.schemas.agent_run import AgentRunResponse, run_to_response
from app.services.agent_run_service import AgentRunService

router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])


@router.get("", response_model=list[AgentRunResponse])
async def list_agent_runs(
    _user: CurrentUser,
    session: SessionDependency,
    task_id: UUID | None = None,
    agent_id: UUID | None = None,
    campaign_id: UUID | None = None,
    status: AgentRunStatus | None = None,
) -> list[AgentRunResponse]:
    runs = await AgentRunService(session).list_runs(
        task_id=task_id, agent_id=agent_id, campaign_id=campaign_id, status=status
    )
    return [run_to_response(item) for item in runs]


@router.get("/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(
    run_id: UUID, user: CurrentUser, session: SessionDependency
) -> AgentRunResponse:
    return run_to_response(
        await AgentRunService(session).get_run(run_id), include_prompt=user.role is UserRole.ADMIN
    )
