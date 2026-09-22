import asyncio
from dataclasses import dataclass
from typing import Any

from agents import RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from pydantic import ValidationError

from app.agents.factory import AgentRuntimeContext, AgentSnapshot, create_runtime_agent
from app.agents.output_registry import output_type_registry
from app.agents.tool_registry import tool_registry
from app.core.config import settings
from app.models.task import TaskType


@dataclass(frozen=True)
class RuntimeResult:
    output_data: dict[str, Any]
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    trace_id: str | None
    openai_response_id: str | None = None


class AgentRuntimeError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class AgentRunnerService:
    async def run(
        self,
        snapshot: AgentSnapshot,
        task_input: str,
        context: AgentRuntimeContext,
        trace_id: str | None,
    ) -> RuntimeResult:
        if not settings.openai_api_key:
            raise AgentRuntimeError("OPENAI_NOT_CONFIGURED", "OpenAI API не настроен.")
        tools = tool_registry.resolve(snapshot.enabled_tool_names)
        sdk_agent = create_runtime_agent(snapshot, tools)
        config = RunConfig(
            workflow_name="iTeam AI Marketing Task",
            group_id=str(context.campaign_id),
            trace_id=trace_id,
            trace_metadata={
                "campaign_id": str(context.campaign_id),
                "task_id": str(context.task_id),
                "agent_id": str(context.agent_id),
                "agent_run_id": str(context.agent_run_id),
            },
            tracing_disabled=settings.openai_agents_disable_tracing,
            trace_include_sensitive_data=False,
        )
        try:
            result = await asyncio.wait_for(
                Runner.run(
                    sdk_agent,
                    task_input,
                    context=context,
                    max_turns=settings.agent_max_turns,
                    run_config=config,
                ),
                timeout=settings.agent_run_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AgentRuntimeError("AGENT_TIMEOUT", "Превышено время выполнения агента.") from exc
        except MaxTurnsExceeded as exc:
            raise AgentRuntimeError(
                "AGENT_MAX_TURNS_EXCEEDED", "Превышено число шагов агента."
            ) from exc
        except ModelBehaviorError as exc:
            raise AgentRuntimeError(
                "AGENT_MODEL_BEHAVIOR_ERROR", "Модель вернула недопустимый результат."
            ) from exc
        except AgentRuntimeError:
            raise
        except Exception as exc:
            raise AgentRuntimeError(
                "AGENT_PROVIDER_ERROR", "Не удалось выполнить запрос к AI-провайдеру."
            ) from exc
        usage = result.context_wrapper.usage
        try:
            output_data = output_type_registry.normalize(
                result.final_output, context.output_task_type or context.task_type
            )
        except ValidationError as exc:
            error_code = {
                TaskType.CAMPAIGN_PLANNING: "INVALID_CAMPAIGN_PLAN",
                TaskType.KNOWLEDGE_RESEARCH: "INVALID_KNOWLEDGE_RESEARCH_RESULT",
                TaskType.WRITE_ARTICLE: "INVALID_ARTICLE_RESULT",
                TaskType.CREATE_SOCIAL_POSTS: "INVALID_SOCIAL_POST_RESULT",
            }.get(context.output_task_type or context.task_type, "INVALID_AGENT_OUTPUT")
            raise AgentRuntimeError(
                error_code, "Структура результата агента не прошла проверку."
            ) from exc
        return RuntimeResult(
            output_data=output_data,
            request_count=usage.requests,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            trace_id=trace_id,
        )
