from dataclasses import dataclass
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
