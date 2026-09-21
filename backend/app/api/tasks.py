from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.dependencies import CurrentUser, SessionDependency
from app.models.task import TaskPriority, TaskStatus, TaskType
from app.schemas.agent_run import AgentRunSummary
from app.schemas.task import (
    TaskCompleteRequest,
    TaskCreate,
    TaskDependencyCreate,
    TaskListItem,
    TaskResponse,
    TaskUpdate,
    task_to_list_item,
    task_to_response,
)
from app.services.agent_run_service import AgentRunService
from app.services.task_service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskListItem])
async def list_tasks(
    _user: CurrentUser,
    session: SessionDependency,
    campaign_id: UUID | None = None,
    agent_id: UUID | None = None,
    task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
    task_type: TaskType | None = None,
    priority: TaskPriority | None = None,
) -> list[TaskListItem]:
    tasks = await TaskService(session).list_tasks(
        campaign_id=campaign_id,
        agent_id=agent_id,
        status=task_status,
        task_type=task_type,
        priority=priority,
    )
    return [task_to_list_item(task) for task in tasks]


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate, _user: CurrentUser, session: SessionDependency
) -> TaskResponse:
    return task_to_response(await TaskService(session).create_task(payload))


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: UUID, _user: CurrentUser, session: SessionDependency) -> TaskResponse:
    return task_to_response(await TaskService(session).get_task(task_id))


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: UUID, payload: TaskUpdate, _user: CurrentUser, session: SessionDependency
) -> TaskResponse:
    return task_to_response(await TaskService(session).update_task(task_id, payload))


@router.post("/{task_id}/start", response_model=TaskResponse)
async def start_task(task_id: UUID, _user: CurrentUser, session: SessionDependency) -> TaskResponse:
    return task_to_response(await TaskService(session).start_task(task_id))


@router.post("/{task_id}/complete", response_model=TaskResponse)
async def complete_task(
    task_id: UUID, payload: TaskCompleteRequest, _user: CurrentUser, session: SessionDependency
) -> TaskResponse:
    return task_to_response(await TaskService(session).complete_task(task_id, payload.output_data))


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: UUID, _user: CurrentUser, session: SessionDependency
) -> TaskResponse:
    return task_to_response(await TaskService(session).cancel_task(task_id))


@router.post("/{task_id}/run", response_model=AgentRunSummary, status_code=status.HTTP_202_ACCEPTED)
async def run_task(
    task_id: UUID, _user: CurrentUser, session: SessionDependency
) -> AgentRunSummary:
    service = AgentRunService(session)
    run = await service.enqueue(await service.create_queued_run(task_id))
    return AgentRunSummary.model_validate(
        {
            "id": run.id,
            "task_id": run.task_id,
            "agent_id": run.agent_id,
            "campaign_id": run.campaign_id,
            "status": run.status,
            "model": run.model,
            "created_at": run.created_at,
        }
    )


@router.post(
    "/{task_id}/retry", response_model=AgentRunSummary, status_code=status.HTTP_202_ACCEPTED
)
async def retry_task(
    task_id: UUID, _user: CurrentUser, session: SessionDependency
) -> AgentRunSummary:
    service = AgentRunService(session)
    run = await service.enqueue(await service.create_queued_run(task_id, retry=True))
    return AgentRunSummary.model_validate(
        {
            "id": run.id,
            "task_id": run.task_id,
            "agent_id": run.agent_id,
            "campaign_id": run.campaign_id,
            "status": run.status,
            "model": run.model,
            "created_at": run.created_at,
        }
    )


@router.post(
    "/{task_id}/dependencies", response_model=TaskResponse, status_code=status.HTTP_201_CREATED
)
async def add_dependency(
    task_id: UUID, payload: TaskDependencyCreate, _user: CurrentUser, session: SessionDependency
) -> TaskResponse:
    return task_to_response(
        await TaskService(session).add_dependency(task_id, payload.depends_on_task_id)
    )


@router.delete("/{task_id}/dependencies/{dependency_id}", response_model=TaskResponse)
async def remove_dependency(
    task_id: UUID, dependency_id: UUID, _user: CurrentUser, session: SessionDependency
) -> TaskResponse:
    return task_to_response(await TaskService(session).remove_dependency(task_id, dependency_id))
