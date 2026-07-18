"""Bounded model-turn orchestration for isolated mutation generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from naumi_agent.evolution.experiment_leases import ExperimentWorktreeLease
from naumi_agent.evolution.experiment_snapshots import EvolutionExperimentSourceSnapshot
from naumi_agent.evolution.experiments import EvolutionExperimentContract
from naumi_agent.evolution.mutation_generation import (
    EvolutionMutationGenerationError,
    EvolutionMutationGenerationResult,
    EvolutionMutationGenerationService,
)
from naumi_agent.evolution.mutation_plans import EvolutionMutationPlan
from naumi_agent.model.router import ModelResponse, ModelTier, TokenUsage
from naumi_agent.runtime.ports.events import RuntimeEvent, RuntimeEventType
from naumi_agent.runtime.ports.model import ModelPort
from naumi_agent.tools.base import ToolCall, ToolResult

logger = logging.getLogger(__name__)

_MAX_SOURCE_CHARS = 2 * 1024 * 1024
_MIN_CONTEXT_TOKENS = 4_096
_PROMPT_TOKEN_RESERVE = 1_024
_READY_MARKER = "MUTATION_READY"


class MutationTurnEventPublisher(Protocol):
    async def publish(
        self,
        event_type: RuntimeEventType,
        data: Mapping[str, object],
        *,
        turn: int = 0,
    ) -> RuntimeEvent: ...


@dataclass(frozen=True, slots=True)
class MutationTurnBudget:
    max_turns: int = 50
    timeout_seconds: float = 300.0
    max_output_tokens: int = 8_192
    max_total_tokens: int = 200_000
    max_prompt_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if isinstance(self.max_turns, bool) or not 1 <= self.max_turns <= 50:
            raise ValueError("Mutation Turn max_turns 必须在 1..50。")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0.01 <= float(self.timeout_seconds) <= 1_800
        ):
            raise ValueError("Mutation Turn timeout_seconds 必须在 0.01..1800。")
        if isinstance(self.max_output_tokens, bool) or not 256 <= self.max_output_tokens <= 65_536:
            raise ValueError("Mutation Turn max_output_tokens 必须在 256..65536。")
        if (
            isinstance(self.max_total_tokens, bool)
            or not 1_024 <= self.max_total_tokens <= 2_000_000
        ):
            raise ValueError("Mutation Turn max_total_tokens 必须在 1024..2000000。")
        if (
            isinstance(self.max_prompt_bytes, bool)
            or not 16_384 <= self.max_prompt_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("Mutation Turn max_prompt_bytes 必须在 16 KiB..4 MiB。")


@dataclass(frozen=True, slots=True)
class EvolutionMutationTurnResult:
    generation: EvolutionMutationGenerationResult
    turns: int
    model_calls: int
    tool_calls: int
    usage: TokenUsage
    models: tuple[str, ...]
    event_delivery_failed: bool = False


class EvolutionMutationTurnError(RuntimeError):
    """Typed runner failure that excludes prompt, source, and model output."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvolutionMutationTurnRunner:
    """Drive one ModelPort through the virtual mutation tool boundary."""

    def __init__(
        self,
        *,
        model_port: ModelPort,
        generation_service: EvolutionMutationGenerationService,
    ) -> None:
        self._model_port = model_port
        self._generation_service = generation_service

    async def run(
        self,
        *,
        contract: EvolutionExperimentContract,
        lease: ExperimentWorktreeLease,
        source_snapshot: EvolutionExperimentSourceSnapshot,
        mutation_plan: EvolutionMutationPlan,
        run_id: str,
        attempt: int,
        model: str | None = None,
        budget: MutationTurnBudget | None = None,
        cancel_event: asyncio.Event | None = None,
        events: MutationTurnEventPublisher | None = None,
    ) -> EvolutionMutationTurnResult:
        limits = budget or MutationTurnBudget()
        cancellation = cancel_event or asyncio.Event()
        if cancellation.is_set():
            raise EvolutionMutationTurnError(
                "mutation_turn_cancelled",
                "Mutation Turn 在启动前已取消。",
            )
        try:
            session = self._generation_service.begin(
                contract=contract,
                lease=lease,
                source_snapshot=source_snapshot,
                mutation_plan=mutation_plan,
                run_id=run_id,
                attempt=attempt,
            )
        except EvolutionMutationGenerationError as exc:
            raise EvolutionMutationTurnError(exc.code, str(exc)) from exc
        try:
            resolved_model = model or self._model_port.resolve_model(ModelTier.CAPABLE)
            context_window = self._safe_model_limit(
                "context window",
                self._model_port.get_context_window(resolved_model),
            )
            max_model_output = self._safe_model_limit(
                "max output",
                self._model_port.get_max_output(resolved_model),
            )
        except EvolutionMutationTurnError:
            raise
        except Exception as exc:
            raise EvolutionMutationTurnError(
                "mutation_turn_model_metadata_failed",
                "无法读取 Mutation Turn 模型元数据。",
            ) from exc
        output_tokens = min(
            limits.max_output_tokens,
            max_model_output,
            max(256, context_window // 4),
        )
        prompt_limit = min(
            limits.max_prompt_bytes,
            max(
                0,
                (context_window - output_tokens - _PROMPT_TOKEN_RESERVE) * 2,
            ),
        )
        if context_window < _MIN_CONTEXT_TOKENS or prompt_limit < 16_384:
            raise EvolutionMutationTurnError(
                "mutation_turn_context_insufficient",
                "当前模型上下文不足以安全执行 Mutation Turn。",
            )
        messages = _initial_messages(
            mutation_plan,
            session.prompt_baseline_contents(),
        )
        _require_prompt_budget(messages, prompt_limit)
        tools = _mutation_tool_schemas(mutation_plan.authorized_files)
        deadline = asyncio.get_running_loop().time() + float(limits.timeout_seconds)
        usage = TokenUsage()
        models: list[str] = []
        tool_call_count = 0

        try:
            await _publish(
                events,
                RuntimeEventType.TURN_START,
                {
                    "domain": "mutation_generation",
                    "mutation_plan_id": mutation_plan.plan_id,
                    "attempt": attempt,
                    "max_turns": limits.max_turns,
                    "timeout_seconds": float(limits.timeout_seconds),
                },
                turn=0,
            )
            for turn in range(1, limits.max_turns + 1):
                _require_prompt_budget(messages, prompt_limit)
                response = await _call_model_with_controls(
                    self._model_port,
                    messages=messages,
                    tools=tools,
                    model=resolved_model,
                    max_tokens=output_tokens,
                    cancel_event=cancellation,
                    deadline=deadline,
                )
                usage = _add_usage(usage, response.usage)
                if usage.total_tokens > limits.max_total_tokens:
                    raise EvolutionMutationTurnError(
                        "mutation_turn_token_budget_exceeded",
                        "Mutation Turn 已超过总 Token 预算。",
                    )
                models.append(response.model or resolved_model)
                calls = _parse_model_tool_calls(response.tool_calls)
                if not calls:
                    try:
                        generation = await session.finalize()
                    except EvolutionMutationGenerationError as exc:
                        raise EvolutionMutationTurnError(
                            "mutation_turn_scope_incomplete",
                            "模型在覆盖完整 approved scope 前结束 Mutation Turn。",
                        ) from exc
                    return await self._complete(
                        generation=generation,
                        turns=turn,
                        model_calls=turn,
                        tool_calls=tool_call_count,
                        usage=usage,
                        models=models,
                        events=events,
                    )

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.content or None,
                    "tool_calls": response.tool_calls,
                }
                if response.reasoning_content:
                    assistant_message["reasoning_content"] = response.reasoning_content
                messages.append(assistant_message)
                for call in calls:
                    tool_call_count += 1
                    call_digest = hashlib.sha256(call.id.encode("utf-8")).hexdigest()
                    await _publish(
                        events,
                        RuntimeEventType.TOOL_START,
                        {
                            "domain": "mutation_generation",
                            "tool_name": call.name,
                            "call_id_sha256": call_digest,
                            "mutation_plan_id": mutation_plan.plan_id,
                        },
                        turn=turn,
                    )
                    result = await session.execute(call)
                    await _publish(
                        events,
                        (
                            RuntimeEventType.TOOL_END
                            if result.status == "success"
                            else RuntimeEventType.TOOL_ERROR
                        ),
                        {
                            "domain": "mutation_generation",
                            "tool_name": call.name,
                            "call_id_sha256": call_digest,
                            "status": result.status,
                            "error_code": _result_error_code(result),
                        },
                        turn=turn,
                    )
                    messages.append(_tool_result_message(result))

                try:
                    generation = await session.finalize()
                except EvolutionMutationGenerationError as exc:
                    if exc.code != "mutation_scope_incomplete":
                        raise EvolutionMutationTurnError(exc.code, str(exc)) from exc
                else:
                    return await self._complete(
                        generation=generation,
                        turns=turn,
                        model_calls=turn,
                        tool_calls=tool_call_count,
                        usage=usage,
                        models=models,
                        events=events,
                    )
            raise EvolutionMutationTurnError(
                "mutation_turn_limit_exceeded",
                "Mutation Turn 已达到 50 轮以内的配置上限。",
            )
        except EvolutionMutationTurnError as exc:
            await _publish_error_best_effort(
                events,
                code=exc.code,
                plan_id=mutation_plan.plan_id,
            )
            raise
        except asyncio.CancelledError:
            await _publish_error_best_effort(
                events,
                code="mutation_turn_caller_cancelled",
                plan_id=mutation_plan.plan_id,
            )
            raise

    async def _complete(
        self,
        *,
        generation: EvolutionMutationGenerationResult,
        turns: int,
        model_calls: int,
        tool_calls: int,
        usage: TokenUsage,
        models: list[str],
        events: MutationTurnEventPublisher | None,
    ) -> EvolutionMutationTurnResult:
        event_delivery_failed = False
        try:
            await _publish(
                events,
                RuntimeEventType.RESPONSE_END,
                {
                    "domain": "mutation_generation",
                    "status": "completed",
                    "trace_id": generation.trace.trace_id,
                    "trace_sha256": generation.trace.trace_sha256,
                    "turns": turns,
                    "tool_calls": tool_calls,
                    "total_tokens": usage.total_tokens,
                },
                turn=turns,
            )
        except Exception:
            logger.exception("Mutation Turn completion event delivery failed")
            event_delivery_failed = True
        return EvolutionMutationTurnResult(
            generation=generation,
            turns=turns,
            model_calls=model_calls,
            tool_calls=tool_calls,
            usage=usage,
            models=tuple(dict.fromkeys(models)),
            event_delivery_failed=event_delivery_failed,
        )

    @staticmethod
    def _safe_model_limit(name: str, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise EvolutionMutationTurnError(
                "mutation_turn_model_metadata_invalid",
                f"模型 {name} 元数据无效。",
            )
        return value


async def _call_model_with_controls(
    model_port: ModelPort,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    cancel_event: asyncio.Event,
    deadline: float,
) -> ModelResponse:
    if cancel_event.is_set():
        raise EvolutionMutationTurnError(
            "mutation_turn_cancelled",
            "Mutation Turn 已取消。",
        )
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise EvolutionMutationTurnError(
            "mutation_turn_timeout",
            "Mutation Turn 已超过总时间预算。",
        )
    model_task = asyncio.create_task(model_port.call(
        messages,
        model=model,
        tier=ModelTier.CAPABLE,
        tools=tools,
        max_tokens=max_tokens,
        temperature=0.0,
    ))
    cancellation_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {model_task, cancellation_task},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in done and cancellation_task.result():
            await _cancel_and_wait(model_task)
            raise EvolutionMutationTurnError(
                "mutation_turn_cancelled",
                "Mutation Turn 已取消。",
            )
        if model_task not in done:
            await _cancel_and_wait(model_task)
            raise EvolutionMutationTurnError(
                "mutation_turn_timeout",
                "Mutation Turn 已超过总时间预算。",
            )
        try:
            response = model_task.result()
        except EvolutionMutationTurnError:
            raise
        except Exception as exc:
            raise EvolutionMutationTurnError(
                "mutation_turn_model_call_failed",
                "Mutation Turn 模型调用失败。",
            ) from exc
        if not isinstance(response, ModelResponse):
            raise EvolutionMutationTurnError(
                "mutation_turn_model_response_invalid",
                "模型返回了无效 Mutation Turn 响应。",
            )
        return response
    except asyncio.CancelledError:
        await _cancel_and_wait(model_task)
        raise
    finally:
        await _cancel_and_wait(cancellation_task)


async def _cancel_and_wait(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _initial_messages(
    plan: EvolutionMutationPlan,
    baseline: Mapping[str, str | None],
) -> list[dict[str, Any]]:
    payload = {
        "mutation_plan_id": plan.plan_id,
        "objective": {
            "finding_code": plan.objective.finding_code,
            "scope": plan.objective.scope,
            "hypothesis": plan.objective.hypothesis,
        },
        "approved_paths": list(plan.authorized_files),
        "files": [
            {
                "path": path,
                "operation": next(
                    item.change_mode for item in plan.planned_files if item.path == path
                ),
                "content": baseline[path],
            }
            for path in plan.authorized_files
        ],
        "completion_marker": _READY_MARKER,
    }
    return [
        {
            "role": "system",
            "content": (
                "你是 NaumiAgent 的隔离变异生成器。只能调用提供的 file_edit/file_write 虚拟工具；"
                "不得请求 shell、网络、磁盘或扩大 approved_paths。"
                "必须让每个 approved path 至少成功更新一次。"
                "工具只修改内存草稿。完成后回复 MUTATION_READY，不要回显完整源码。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]


def _mutation_tool_schemas(paths: tuple[str, ...]) -> list[dict[str, Any]]:
    path_schema = {"type": "string", "enum": list(paths)}
    return [
        {
            "type": "function",
            "function": {
                "name": "file_edit",
                "description": "精确替换一个 approved file 中唯一出现的文本。",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "old_text", "new_text"],
                    "properties": {
                        "path": path_schema,
                        "old_text": {"type": "string", "maxLength": _MAX_SOURCE_CHARS},
                        "new_text": {"type": "string", "maxLength": _MAX_SOURCE_CHARS},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "file_write",
                "description": "创建或完整覆盖一个 approved file 的内存草稿。",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "content"],
                    "properties": {
                        "path": path_schema,
                        "content": {"type": "string", "maxLength": _MAX_SOURCE_CHARS},
                    },
                },
            },
        },
    ]


def _parse_model_tool_calls(raw_calls: object) -> tuple[ToolCall, ...]:
    if not isinstance(raw_calls, list):
        raise EvolutionMutationTurnError(
            "mutation_turn_tool_protocol_invalid",
            "模型 tool_calls 必须是列表。",
        )
    parsed: list[ToolCall] = []
    for raw in raw_calls:
        if not isinstance(raw, dict) or set(raw) != {"id", "type", "function"}:
            raise EvolutionMutationTurnError(
                "mutation_turn_tool_protocol_invalid",
                "模型 tool call 字段不完整或包含额外字段。",
            )
        function = raw["function"]
        if (
            raw["type"] != "function"
            or not isinstance(raw["id"], str)
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
            or not isinstance(function["name"], str)
            or not isinstance(function["arguments"], str)
        ):
            raise EvolutionMutationTurnError(
                "mutation_turn_tool_protocol_invalid",
                "模型 tool call 格式无效。",
            )
        parsed.append(ToolCall(
            id=raw["id"],
            name=function["name"],
            arguments=function["arguments"],
        ))
    return tuple(parsed)


def _tool_result_message(result: ToolResult) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": result.call_id,
        "content": result.content,
    }


def _result_error_code(result: ToolResult) -> str:
    prefix = "Mutation proposal 未更新："
    if result.status != "error" or not result.content.startswith(prefix):
        return ""
    code = result.content.removeprefix(prefix)
    return code if code.isascii() and len(code) <= 128 else "mutation_tool_error"


def _require_prompt_budget(messages: list[dict[str, Any]], limit: int) -> None:
    encoded = json.dumps(
        messages,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > limit:
        raise EvolutionMutationTurnError(
            "mutation_turn_prompt_oversized",
            "Mutation Turn 上下文超过模型与安全预算，未截断 approved source。",
        )


def _add_usage(current: TokenUsage, added: TokenUsage) -> TokenUsage:
    counters = (
        added.input_tokens,
        added.output_tokens,
        added.total_tokens,
        added.cache_tokens,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counters
    ) or (
        isinstance(added.cost_usd, bool)
        or not isinstance(added.cost_usd, (int, float))
        or not math.isfinite(float(added.cost_usd))
        or added.cost_usd < 0
    ) or added.total_tokens != added.input_tokens + added.output_tokens:
        raise EvolutionMutationTurnError(
            "mutation_turn_usage_invalid",
            "模型返回了无效 Token usage。",
        )
    return TokenUsage(
        input_tokens=current.input_tokens + added.input_tokens,
        output_tokens=current.output_tokens + added.output_tokens,
        total_tokens=current.total_tokens + added.total_tokens,
        cost_usd=round(current.cost_usd + added.cost_usd, 6),
        cache_tokens=current.cache_tokens + added.cache_tokens,
    )


async def _publish(
    publisher: MutationTurnEventPublisher | None,
    event_type: RuntimeEventType,
    data: Mapping[str, object],
    *,
    turn: int,
) -> None:
    if publisher is None:
        return
    try:
        await publisher.publish(event_type, data, turn=turn)
    except Exception as exc:
        raise EvolutionMutationTurnError(
            "mutation_turn_event_delivery_failed",
            "Mutation Turn Runtime Event 无法交付。",
        ) from exc


async def _publish_error_best_effort(
    publisher: MutationTurnEventPublisher | None,
    *,
    code: str,
    plan_id: str,
) -> None:
    try:
        await _publish(
            publisher,
            RuntimeEventType.ERROR,
            {
                "domain": "mutation_generation",
                "code": code,
                "mutation_plan_id": plan_id,
            },
            turn=0,
        )
    except EvolutionMutationTurnError:
        logger.warning("Mutation Turn error event delivery failed: %s", code)


__all__ = [
    "EvolutionMutationTurnError",
    "EvolutionMutationTurnResult",
    "EvolutionMutationTurnRunner",
    "MutationTurnBudget",
    "MutationTurnEventPublisher",
]
