from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.task import TaskPriority, TaskStatus, TaskType


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: UUID
    parent_task_id: UUID | None = None
    task_type: TaskType = TaskType.MANUAL
    title: str = Field(max_length=255)
    description: str | None = None
    assigned_agent_id: UUID | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    input_data: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    deadline: datetime | None = None
    dependency_ids: list[UUID] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Название задачи обязательно.")
        return value

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=255)
    description: str | None = None
    assigned_agent_id: UUID | None = None
    priority: TaskPriority | None = None
    deadline: datetime | None = None
    requires_approval: bool | None = None
    input_data: dict[str, Any] | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            raise ValueError("Название задачи обязательно.")
        return value.strip()


class TaskCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_data: dict[str, Any] = Field(default_factory=dict)


class TaskDependencyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    depends_on_task_id: UUID


class TaskAgentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    slug: str


class TaskCampaignSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str


class TaskReference(BaseModel):
    id: UUID
    title: str
    status: TaskStatus


class TaskListItem(BaseModel):
    id: UUID
    campaign_id: UUID
    campaign: TaskCampaignSummary
    task_type: TaskType
    title: str
    assigned_agent: TaskAgentSummary | None
    priority: TaskPriority
    status: TaskStatus
    deadline: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskResponse(TaskListItem):
    parent_task_id: UUID | None
    parent_task: TaskReference | None
    description: str | None
    assigned_agent_id: UUID | None
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    requires_approval: bool
    error_message: str | None
    retry_count: int
    started_at: datetime | None
    completed_at: datetime | None
    dependencies: list[TaskReference]
    dependents: list[TaskReference]


class TaskDependencyResponse(TaskReference):
    pass


def task_to_response(task: Any) -> TaskResponse:
    return TaskResponse(
        id=task.id,
        campaign_id=task.campaign_id,
        campaign=TaskCampaignSummary.model_validate(task.campaign),
        parent_task_id=task.parent_task_id,
        parent_task=TaskReference(
            id=task.parent_task.id, title=task.parent_task.title, status=task.parent_task.status
        )
        if task.parent_task
        else None,
        task_type=task.task_type,
        title=task.title,
        description=task.description,
        assigned_agent_id=task.assigned_agent_id,
        assigned_agent=TaskAgentSummary.model_validate(task.assigned_agent)
        if task.assigned_agent
        else None,
        priority=task.priority,
        status=task.status,
        input_data=task.input_data,
        output_data=task.output_data,
        requires_approval=task.requires_approval,
        error_message=task.error_message,
        retry_count=task.retry_count,
        deadline=task.deadline,
        started_at=task.started_at,
        completed_at=task.completed_at,
        dependencies=[
            TaskReference(
                id=link.depends_on_task.id,
                title=link.depends_on_task.title,
                status=link.depends_on_task.status,
            )
            for link in task.dependencies
        ],
        dependents=[
            TaskReference(id=link.task.id, title=link.task.title, status=link.task.status)
            for link in task.dependent_links
        ],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def task_to_list_item(task: Any) -> TaskListItem:
    response = task_to_response(task)
    return TaskListItem.model_validate(response.model_dump())
