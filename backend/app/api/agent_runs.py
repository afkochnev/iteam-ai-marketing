from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import CurrentUser, SessionDependency
from app.core.errors import AppError
from app.models.agent_run import AgentRunStatus
from app.models.marketing_chat import MarketingConversation
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
    visible = []
    for item in runs:
        if (
            item.input_data.get("internal_kind") == "MARKETING_DIRECTOR_CHAT"
            and _user.role is not UserRole.ADMIN
        ):
            from app.models.task import Task

            task = await session.get(Task, item.task_id)
            conversation_id = task.input_data.get("conversation_id") if task else None
            owner = (
                await session.scalar(
                    select(MarketingConversation.created_by_user_id).where(
                        MarketingConversation.id == conversation_id
                    )
                )
                if conversation_id
                else None
            )
            if owner != _user.id:
                continue
        visible.append(run_to_response(item))
    return visible


@router.get("/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(
    run_id: UUID, user: CurrentUser, session: SessionDependency
) -> AgentRunResponse:
    run = await AgentRunService(session).get_run(run_id)
    if (
        run.input_data.get("internal_kind") == "MARKETING_DIRECTOR_CHAT"
        and user.role is not UserRole.ADMIN
    ):
        from app.models.task import Task

        task = await session.get(Task, run.task_id)
        conversation_id = task.input_data.get("conversation_id") if task else None
        owner = (
            await session.scalar(
                select(MarketingConversation.created_by_user_id).where(
                    MarketingConversation.id == conversation_id
                )
            )
            if conversation_id
            else None
        )
        if owner != user.id:
            raise AppError("AGENT_RUN_NOT_FOUND", "Запуск не найден.", 404)
    return run_to_response(run, include_prompt=user.role is UserRole.ADMIN)
