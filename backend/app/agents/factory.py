from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agents import Agent


@dataclass(frozen=True)
class AgentRuntimeContext:
    agent_id: UUID
    task_id: UUID
    campaign_id: UUID
    agent_run_id: UUID


@dataclass(frozen=True)
class AgentSnapshot:
    name: str
    prompt: str
    model: str
    enabled_tool_names: list[str]


def create_runtime_agent(snapshot: AgentSnapshot, tools: list[Any]) -> Agent[AgentRuntimeContext]:
    return Agent(
        name=snapshot.name, instructions=snapshot.prompt, model=snapshot.model, tools=tools
    )
