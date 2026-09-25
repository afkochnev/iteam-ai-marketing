from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agents import Agent
from pydantic import BaseModel

from app.models.task import TaskType


@dataclass(frozen=True)
class AgentRuntimeContext:
    agent_id: UUID
    task_id: UUID
    campaign_id: UUID
    agent_run_id: UUID
    task_type: TaskType = TaskType.MANUAL
    allowed_knowledge_pack_ids: tuple[UUID, ...] = ()
    allowed_content_version_ids: tuple[UUID, ...] = ()
    output_task_type: TaskType | None = None
    # Celery executions provide a loop-local DB factory for their tools.
    session_factory: Any = None
    # Invocation-local immutable tool results; avoids re-reading the same source
    # during a bounded structured-output repair turn.
    content_version_cache: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSnapshot:
    name: str
    prompt: str
    model: str
    enabled_tool_names: list[str]
    output_type: type[BaseModel] | None = None


def create_runtime_agent(snapshot: AgentSnapshot, tools: list[Any]) -> Agent[Any]:
    return Agent(
        name=snapshot.name,
        instructions=snapshot.prompt,
        model=snapshot.model,
        tools=tools,
        output_type=snapshot.output_type,
    )
