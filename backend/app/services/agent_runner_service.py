import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agents import OpenAIResponsesModel, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded, ModelBehaviorError
from openai import APITimeoutError, AsyncOpenAI
from pydantic import ValidationError

from app.agents.factory import AgentRuntimeContext, AgentSnapshot, create_runtime_agent
from app.agents.output_registry import output_type_registry
from app.agents.tool_registry import tool_registry
from app.core.config import settings
from app.core.redaction import redact_text
from app.models.task import TaskType
from app.schemas.agent_outputs import SingleSocialPostResult

logger = logging.getLogger(__name__)
MAX_OUTPUT_REPAIR_ATTEMPTS = 2


class _InstrumentedResponsesModel(OpenAIResponsesModel):
    """Safe per-turn diagnostics without logging prompts or article bodies."""

    def __init__(
        self,
        *args: Any,
        agent_run_id: str,
        task_id: str,
        normal_timeout: float,
        final_timeout: float,
        deadline: float,
        repair: bool,
        session_factory: Any = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self._agent_run_id = agent_run_id
        self._task_id = task_id
        self._turn_index = 0
        self._normal_timeout = normal_timeout
        self._final_timeout = final_timeout
        self._deadline = deadline
        self._repair = repair
        self._session_factory = session_factory

    async def _record_external_request(self) -> None:
        """Persist per-turn accounting independently of the main worker session."""

        if self._session_factory is None:
            return
        from sqlalchemy import select

        from app.models.agent_run import AgentRun

        async with self._session_factory() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == self._agent_run_id).with_for_update()
            )
            if run is None:
                return
            accounting = dict((run.input_data or {}).get("model_request_accounting") or {})
            accounting["external_model_request_count"] = (
                int(accounting.get("external_model_request_count", 0)) + 1
            )
            accounting["sdk_turn_count"] = int(accounting.get("sdk_turn_count", 0)) + 1
            accounting["last_model_request_at"] = datetime.now(UTC).isoformat()
            run.input_data = {**run.input_data, "model_request_accounting": accounting}
            await session.commit()

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        self._turn_index += 1
        turn = self._turn_index
        await self._record_external_request()
        started = asyncio.get_running_loop().time()
        configured_tools = kwargs.get("tools")
        if configured_tools is None and len(args) > 3:
            configured_tools = args[3]
        phase = (
            "REPAIR" if self._repair else ("TOOL_SELECTION" if turn == 1 else "FINAL_GENERATION")
        )
        effective_timeout = min(
            self._final_timeout if phase != "TOOL_SELECTION" else self._normal_timeout,
            max(0.1, self._deadline - asyncio.get_running_loop().time() - 0.1),
        )
        # Agents SDK forwards ModelSettings.extra_args directly to
        # responses.create().  Passing timeout here is the actual per-request
        # HTTPX override; mutating the shared client's default timeout is not
        # sufficient because the SDK keeps its own request options object.
        model_settings = kwargs.get("model_settings")
        if model_settings is None and len(args) > 2:
            model_settings = args[2]
        if model_settings is not None:
            extra_args = dict(getattr(model_settings, "extra_args", None) or {})
            extra_args["timeout"] = effective_timeout
            model_settings.extra_args = extra_args
        logger.info(
            "Agent model turn started",
            extra={
                "event": "agent_model_turn_started",
                "agent_run_id": self._agent_run_id,
                "task_id": self._task_id,
                "turn_index": turn,
                "phase": phase,
                "configured_timeout_seconds": (
                    self._final_timeout if phase != "TOOL_SELECTION" else self._normal_timeout
                ),
                "effective_timeout_seconds": effective_timeout,
                "actual_request_timeout_seconds": effective_timeout,
                "tool_calls_requested": len(configured_tools or []),
            },
        )
        try:
            response = await super().get_response(*args, **kwargs)
            response_output = getattr(response, "output", ()) or ()
            tool_calls_requested = sum(
                1
                for item in response_output
                if getattr(item, "type", None) in {"function_call", "computer_call"}
            )
            logger.info(
                "Agent model turn completed",
                extra={
                    "event": "agent_model_turn_completed",
                    "agent_run_id": self._agent_run_id,
                    "task_id": self._task_id,
                    "turn_index": turn,
                    "tool_calls_requested": tool_calls_requested,
                    "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000, 2),
                },
            )
            return response
        except BaseException as exc:
            logger.exception(
                "Agent model turn failed",
                extra={
                    "event": "agent_model_turn_failed",
                    "agent_run_id": self._agent_run_id,
                    "task_id": self._task_id,
                    "turn_index": turn,
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000, 2),
                },
            )
            raise


def _is_structured_output_error(error: ModelBehaviorError) -> bool:
    """Recognize SDK schema validation, but not unrelated model behavior errors."""

    return isinstance(error.__cause__, ValidationError) and error.message.startswith(
        "Invalid JSON when parsing"
    )


def _structured_validation_reason(error: ModelBehaviorError | ValidationError) -> str:
    cause = error if isinstance(error, ValidationError) else error.__cause__
    if isinstance(cause, ValidationError):
        parts = []
        for detail in cause.errors(include_context=False):
            location = ".".join(str(item) for item in detail.get("loc", ())) or "output"
            parts.append(f"{location}: {detail.get('msg', 'invalid value')}")
        return "; ".join(parts)[:2000]
    return "Структура ответа не прошла проверку."


def _repair_input(
    original_input: str,
    error: ModelBehaviorError | ValidationError,
    task_type: TaskType,
    cached_source_content: str | None = None,
    *,
    single_plan_item: bool = False,
) -> str:
    guidance = ""
    if task_type is TaskType.CAMPAIGN_PLANNING:
        guidance = (
            " Допустимые соответствия задач: KNOWLEDGE_RESEARCH -> knowledge_keeper;"
            " WRITE_ARTICLE -> writer; CREATE_SOCIAL_POSTS -> smm_manager."
            " Не добавляй другие типы задач или agent_slug."
        )
    if task_type is TaskType.CREATE_SOCIAL_POSTS:
        if single_plan_item:
            guidance = (
                " Контракт одиночного поста для Publication Plan item (SingleSocialPostResult): "
                "верни sufficient=true и pack с strategy_summary и ровно одним элементом posts; "
                "у поста обязательны key, title, text_markdown, sources и "
                "suggested_publish_order=1. В sources укажи как минимум одну пару "
                "content_version_id и section_key из разрешённой версии статьи. "
                "Не добавляй channel: канал уже определён пунктом утверждённого плана. "
                "Не возвращай несколько постов и не добавляй неизвестные поля. "
                "Если материала недостаточно, верни sufficient=false, pack=null и непустой gaps. "
                "Текст поста должен быть plain text без Markdown-разметки и внутренних меток."
            )
        else:
            expected_count_match = re.search(
                r"['\"]post_count['\"]\s*:\s*(\d+)", original_input
            )
            expected_count = (
                expected_count_match.group(1) if expected_count_match else "из снимка стратегии"
            )
            channels_match = re.search(
                r"['\"]channels['\"]\s*:\s*\[([^\]]*)\]", original_input
            )
            allowed_channels = channels_match.group(1) if channels_match else "TELEGRAM, VK"
            actual_count = "не определён"
            actual_orders = "не определены"
            duplicate_orders = "[]"
            missing_orders = "не определены"
            cause = error if isinstance(error, ValidationError) else error.__cause__
            if isinstance(cause, ValidationError):
                for detail in cause.errors(include_context=False):
                    value = detail.get("input")
                    if isinstance(value, dict) and isinstance(value.get("posts"), list):
                        posts = value["posts"]
                        orders: list[int] = [
                            int(post["suggested_publish_order"])
                            for post in posts
                            if isinstance(post, dict)
                            and isinstance(post.get("suggested_publish_order"), int)
                        ]
                        actual_count = str(len(posts))
                        actual_orders = str(orders)
                        duplicates = sorted({order for order in orders if orders.count(order) > 1})
                        duplicate_orders = str(duplicates)
                        try:
                            missing_orders = str(
                                sorted(set(range(1, int(expected_count) + 1)) - set(orders))
                            )
                        except ValueError:
                            pass
                        break
            guidance = (
                " Детерминированные требования SocialPostPack: "
                f"expected_post_count={expected_count}; actual_post_count={actual_count}; "
                "expected_publish_orders="
                f"[1..{expected_count}]; actual_publish_orders={actual_orders}; "
                "duplicate_publish_orders="
                f"{duplicate_orders}; missing_publish_orders={missing_orders}; "
                f"allowed_channels=[{allowed_channels}]. "
                "Порядок публикации глобальный для всего пакета: не начинай нумерацию "
                "заново по каналу. "
                "Сгенерируй полный исправленный пакет, а не только недостающие посты; "
                "сохрани смысл статьи и стратегии; верни ровно полный объект. "
                "Текст постов должен быть plain text без **, Markdown-заголовков, code fences, "
                "CTA:/Порядок:/section_key/provenance и иных внутренних меток. "
                "Каждый пост должен быть самостоятельным, цельным и разговорным: начни с "
                "узнаваемой управленческой ситуации или спокойного обращения к читателю, "
                "как опытный консультант iTeam к владельцу бизнеса или команде. Уместны общие "
                "наблюдения («Часто вижу…», «Знакомая ситуация…»), но нельзя выдумывать клиентов, "
                "кейсы, результаты или цитаты. Естественно используй «вы» и «ваша команда», "
                "чередуй наблюдение, мини-ситуацию, контраст, практическую мысль и рефлексивный "
                "вопрос; не повторяй нейтральное начало и одну схему во всех постах. Заверши "
                "естественным выводом или вопросом, не навязывай CTA; CTA не обязателен."
            )
        if cached_source_content:
            guidance += (
                " Статья уже прочитана и приведена ниже; повторно инструмент не вызывай. "
                f"Прочитанный материал: {cached_source_content}"
            )
    return (
        f"{original_input}\n\n"
        "Предыдущий структурированный ответ не прошёл проверку."
        f" Исправь только ошибки валидации: {_structured_validation_reason(error)}."
        f"{guidance} Повтори полный ответ строго в требуемом формате."
    )


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
    async def _record_generation_attempt(
        self,
        context: AgentRuntimeContext,
        *,
        repair: bool,
        max_logical_generations: int,
    ) -> None:
        if context.session_factory is None:
            return
        from sqlalchemy import select

        from app.models.agent_run import AgentRun

        async with context.session_factory() as session:
            run = await session.scalar(
                select(AgentRun).where(AgentRun.id == context.agent_run_id).with_for_update()
            )
            if run is None:
                return
            accounting = dict((run.input_data or {}).get("model_request_accounting") or {})
            logical = int(accounting.get("logical_generation_attempt_count", 0)) + 1
            if logical > max_logical_generations:
                raise AgentRuntimeError(
                    "AGENT_REQUEST_BOUND_EXCEEDED",
                    "Превышен предел логических генераций агента.",
                )
            accounting["logical_generation_attempt_count"] = logical
            if repair:
                accounting["repair_request_count"] = (
                    int(accounting.get("repair_request_count", 0)) + 1
                )
            run.input_data = {**run.input_data, "model_request_accounting": accounting}
            await session.commit()

    async def run(
        self,
        snapshot: AgentSnapshot,
        task_input: str,
        context: AgentRuntimeContext,
        trace_id: str | None,
    ) -> RuntimeResult:
        if not settings.openai_api_key:
            raise AgentRuntimeError("OPENAI_NOT_CONFIGURED", "OpenAI API не настроен.")
        task_type = context.output_task_type or context.task_type
        enabled_tool_names = snapshot.enabled_tool_names
        if task_type is TaskType.CREATE_SOCIAL_POSTS:
            enabled_tool_names = [
                name for name in enabled_tool_names if name == "read_content_version"
            ]
        error_code = {
            TaskType.CAMPAIGN_PLANNING: "INVALID_CAMPAIGN_PLAN",
            TaskType.KNOWLEDGE_RESEARCH: "INVALID_KNOWLEDGE_RESEARCH_RESULT",
            TaskType.WRITE_ARTICLE: "INVALID_ARTICLE_RESULT",
            TaskType.CREATE_SOCIAL_POSTS: "INVALID_SOCIAL_POST_RESULT",
        }.get(task_type, "INVALID_AGENT_OUTPUT")
        current_input = task_input
        deadline = asyncio.get_running_loop().time() + settings.agent_run_timeout_seconds
        max_turns = (
            settings.smm_agent_max_turns
            if task_type is TaskType.CREATE_SOCIAL_POSTS
            else settings.agent_max_turns
        )
        configured_repair_limit = (
            settings.smm_agent_output_repair_attempts
            if task_type is TaskType.CREATE_SOCIAL_POSTS
            else settings.agent_output_repair_attempts
        )
        repair_limit = min(configured_repair_limit, MAX_OUTPUT_REPAIR_ATTEMPTS)
        for repair_attempt in range(repair_limit + 1):
            await self._record_generation_attempt(
                context,
                repair=repair_attempt > 0,
                max_logical_generations=repair_limit + 1,
            )
            attempt_tool_names = enabled_tool_names
            if repair_attempt > 0 and context.content_version_cache:
                attempt_tool_names = []
            tools = tool_registry.resolve(attempt_tool_names)
            sdk_agent = create_runtime_agent(snapshot, tools)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AgentRuntimeError("AGENT_TIMEOUT", "Превышено время выполнения агента.")
            base_provider_timeout = (
                float(settings.smm_final_provider_timeout_seconds)
                if task_type is TaskType.CREATE_SOCIAL_POSTS
                else float(settings.agent_provider_request_timeout_seconds)
            )
            request_timeout = min(
                base_provider_timeout,
                max(0.1, remaining - 0.1),
            )
            provider_client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                timeout=request_timeout,
                max_retries=settings.agent_provider_max_retries,
            )
            sdk_model = _InstrumentedResponsesModel(
                snapshot.model,
                provider_client,
                agent_run_id=str(context.agent_run_id),
                task_id=str(context.task_id),
                normal_timeout=float(settings.agent_provider_request_timeout_seconds),
                final_timeout=(
                    float(settings.smm_final_provider_timeout_seconds)
                    if task_type is TaskType.CREATE_SOCIAL_POSTS
                    else float(settings.agent_provider_request_timeout_seconds)
                ),
                deadline=deadline,
                repair=repair_attempt > 0,
                session_factory=context.session_factory,
            )
            config = RunConfig(
                model=sdk_model,
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
                try:
                    result = await asyncio.wait_for(
                        Runner.run(
                            sdk_agent,
                            current_input,
                            context=context,
                            max_turns=max_turns,
                            run_config=config,
                        ),
                        timeout=remaining,
                    )
                except APITimeoutError as exc:
                    raise AgentRuntimeError(
                        "AGENT_PROVIDER_TIMEOUT",
                        "Истёк тайм-аут запроса к AI-провайдеру.",
                    ) from exc
                except TimeoutError as exc:
                    raise AgentRuntimeError(
                        "AGENT_TIMEOUT", "Превышено время выполнения агента."
                    ) from exc
                except MaxTurnsExceeded as exc:
                    raise AgentRuntimeError(
                        "SMM_MAX_TURNS_EXCEEDED"
                        if task_type is TaskType.CREATE_SOCIAL_POSTS
                        else "AGENT_MAX_TURNS_EXCEEDED",
                        "Превышено число шагов агента.",
                    ) from exc
                except ModelBehaviorError as exc:
                    if _is_structured_output_error(exc):
                        if repair_attempt >= repair_limit:
                            raise AgentRuntimeError(
                                error_code,
                                "Структура результата агента не прошла проверку.",
                            ) from exc
                        logger.info(
                            "Agent structured output repair scheduled",
                            extra={
                                "event": "agent_output_repair_scheduled",
                                "agent_run_id": str(context.agent_run_id),
                                "task_id": str(context.task_id),
                                "repair_attempt": repair_attempt + 1,
                                "validation_reason": _structured_validation_reason(exc),
                            },
                        )
                        current_input = _repair_input(
                            task_input,
                            exc,
                            task_type,
                            cached_source_content=(
                                next(iter(context.content_version_cache.values()), None)
                                if task_type is TaskType.CREATE_SOCIAL_POSTS
                                else None
                            ),
                            single_plan_item=snapshot.output_type is SingleSocialPostResult,
                        )
                        continue
                    raise AgentRuntimeError(
                        "AGENT_MODEL_BEHAVIOR_ERROR",
                        "Модель вернула недопустимый результат.",
                    ) from exc
                except AgentRuntimeError:
                    raise
                except Exception as exc:
                    safe_message = redact_text(str(exc))
                    exc.args = (safe_message,)
                    logger.exception(
                        "Agent provider error",
                        extra={
                            "event": "agent_provider_error",
                            "exception_type": type(exc).__name__,
                            "exception_message": safe_message,
                            "agent_id": str(context.agent_id),
                            "agent_name": snapshot.name,
                            "task_id": str(context.task_id),
                            "agent_run_id": str(context.agent_run_id),
                            "model": snapshot.model,
                            "trace_id": trace_id,
                        },
                    )
                    raise AgentRuntimeError(
                        "AGENT_PROVIDER_ERROR",
                        "Не удалось выполнить запрос к AI-провайдеру.",
                    ) from exc
                usage = result.context_wrapper.usage
                try:
                    output_data = output_type_registry.normalize(
                        result.final_output,
                        task_type,
                        output_type=snapshot.output_type,
                    )
                except ValidationError as exc:
                    if repair_attempt >= repair_limit:
                        raise AgentRuntimeError(
                            error_code, "Структура результата агента не прошла проверку."
                        ) from exc
                    current_input = _repair_input(
                        task_input,
                        exc,
                        task_type,
                        cached_source_content=(
                            next(iter(context.content_version_cache.values()), None)
                            if task_type is TaskType.CREATE_SOCIAL_POSTS
                            else None
                        ),
                        single_plan_item=snapshot.output_type is SingleSocialPostResult,
                    )
                    continue
                return RuntimeResult(
                    output_data=output_data,
                    request_count=usage.requests,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    trace_id=trace_id,
                )
            finally:
                await provider_client.close()
        raise AgentRuntimeError(error_code, "Структура результата агента не прошла проверку.")
