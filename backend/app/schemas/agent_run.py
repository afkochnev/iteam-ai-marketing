from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.models.agent_run import AgentRunStatus, ToolCallStatus


class AgentRunAgentSummary(BaseModel):
    id: UUID
    name: str
    slug: str


class ToolCallResponse(BaseModel):
    id: UUID
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    status: ToolCallStatus
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None


class AgentRunSummary(BaseModel):
    id: UUID
    task_id: UUID
    agent_id: UUID
    campaign_id: UUID
    status: AgentRunStatus
    model: str
    created_at: datetime


class AgentRunResponse(AgentRunSummary):
    agent: AgentRunAgentSummary
    output_data: dict[str, Any] | None
    request_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    trace_id: str | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    prompt_snapshot: str | None = None
    tool_calls: list[ToolCallResponse]


def run_to_response(run: Any, include_prompt: bool = False) -> AgentRunResponse:
    return AgentRunResponse(
        id=run.id,
        task_id=run.task_id,
        agent_id=run.agent_id,
        campaign_id=run.campaign_id,
        status=run.status,
        model=run.model,
        created_at=run.created_at,
        agent=AgentRunAgentSummary(id=run.agent.id, name=run.agent.name, slug=run.agent.slug),
        output_data=run.output_data,
        request_count=run.request_count,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        total_tokens=run.total_tokens,
        trace_id=run.trace_id,
        error_code=run.error_code,
        error_message=run.error_message,
        started_at=run.started_at,
        completed_at=run.completed_at,
        prompt_snapshot=run.prompt_snapshot if include_prompt else None,
        tool_calls=[
            ToolCallResponse.model_validate(
                {
                    "id": item.id,
                    "tool_name": item.tool_name,
                    "arguments": item.arguments,
                    "result": item.result,
                    "status": item.status,
                    "error_message": item.error_message,
                    "started_at": item.started_at,
                    "completed_at": item.completed_at,
                }
            )
            for item in run.tool_calls
        ],
    )
