"""Bounded model-only execution contracts for an independent Agent Worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from naumi_agent.config.settings import ModelConfig
from naumi_agent.daemons.agent_jobs import AgentJobPayload
from naumi_agent.daemons.agent_worker_contract import AgentWorkerRequest
from naumi_agent.model.catalog import load_provider_catalog
from naumi_agent.model.router import ModelResponse, ModelRouter, ModelTier

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_MODEL_PROFILE_BYTES = 2 * 1024**2
MAX_AGENT_MODEL_RESPONSE_BYTES = 16 * 1024**2
MAX_AGENT_MODEL_MESSAGES_BYTES = 64 * 1024**2
MAX_AGENT_MODEL_TOOLS_BYTES = 4 * 1024**2
_MAX_ERROR_BYTES = 4096
_MAX_MODEL_NAME_BYTES = 1024


class AgentWorkerModelExecutionStatus(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    MAX_TURNS = "max_turns"


@dataclass(frozen=True, slots=True)
class AgentWorkerModelExecutionPreparation:
    execution_id: str
    job_id: str
    request_sha256: str
    claim_epoch: int
    model_profile_sha256: str
    tool_manifest_sha256: str

    def __post_init__(self) -> None:
        for name in ("execution_id", "job_id"):
            _require_identifier(getattr(self, name), field=name)
        _require_sha256(self.request_sha256, field="request_sha256")
        _require_positive_int(self.claim_epoch, field="claim_epoch")
        _require_sha256(
            self.model_profile_sha256,
            field="model_profile_sha256",
        )
        _require_sha256(
            self.tool_manifest_sha256,
            field="tool_manifest_sha256",
        )


@dataclass(frozen=True, slots=True)
class AgentWorkerModelExecutionResult:
    schema_version: int
    execution_id: str
    request_sha256: str
    status: AgentWorkerModelExecutionStatus
    response: str = field(repr=False)
    error: str = field(repr=False)
    error_code: str
    total_tokens: int
    total_cost_microusd: int
    turns: int
    model: str
    provider_model: str
    provider_response_id_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Agent Worker model result schema_version 必须为 1。")
        _require_identifier(self.execution_id, field="execution_id")
        _require_sha256(self.request_sha256, field="request_sha256")
        if not isinstance(self.status, AgentWorkerModelExecutionStatus):
            raise TypeError("Agent Worker model result status 类型无效。")
        _require_bounded_text(
            self.response,
            field="response",
            maximum=MAX_AGENT_MODEL_RESPONSE_BYTES,
            allow_empty=True,
        )
        _require_bounded_text(
            self.error,
            field="error",
            maximum=_MAX_ERROR_BYTES,
            allow_empty=True,
        )
        if self.status is AgentWorkerModelExecutionStatus.COMPLETED:
            if self.error or self.error_code:
                raise ValueError("成功的 Agent Worker model result 不得包含错误。")
        else:
            _require_identifier(self.error_code, field="error_code")
            if not self.error:
                raise ValueError("失败的 Agent Worker model result 必须包含安全错误。")
        _require_int_range(
            self.total_tokens,
            field="total_tokens",
            minimum=0,
            maximum=2**63 - 1,
        )
        _require_int_range(
            self.total_cost_microusd,
            field="total_cost_microusd",
            minimum=0,
            maximum=10**18,
        )
        if not 0 <= self.turns <= 1_000:
            raise ValueError("Agent model execution turns 必须在 0 到 1000 之间。")
        _require_bounded_text(
            self.model,
            field="model",
            maximum=_MAX_MODEL_NAME_BYTES,
            allow_empty=self.status is not AgentWorkerModelExecutionStatus.COMPLETED,
        )
        _require_bounded_text(
            self.provider_model,
            field="provider_model",
            maximum=_MAX_MODEL_NAME_BYTES,
            allow_empty=True,
        )
        if self.provider_response_id_sha256:
            _require_sha256(
                self.provider_response_id_sha256,
                field="provider_response_id_sha256",
            )

    @property
    def total_cost_usd(self) -> float:
        return self.total_cost_microusd / 1_000_000


@dataclass(frozen=True, slots=True)
class AgentWorkerModelTurnResult:
    status: AgentWorkerModelExecutionStatus
    response: str = field(repr=False)
    tool_calls: tuple[dict[str, Any], ...] = field(repr=False)
    error: str = field(repr=False)
    error_code: str
    total_tokens: int
    total_cost_microusd: int
    model: str
    provider_model: str
    provider_response_id_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, AgentWorkerModelExecutionStatus):
            raise TypeError("Agent Worker model turn status 类型无效。")
        _require_bounded_text(
            self.response,
            field="response",
            maximum=MAX_AGENT_MODEL_RESPONSE_BYTES,
            allow_empty=True,
        )
        if not isinstance(self.tool_calls, tuple) or any(
            not isinstance(item, dict) for item in self.tool_calls
        ):
            raise TypeError("Agent Worker model turn tool_calls 类型无效。")
        _bounded_json_bytes(
            self.tool_calls,
            maximum=MAX_AGENT_MODEL_TOOLS_BYTES,
            label="model turn tool_calls",
            allow_empty=True,
        )
        _require_bounded_text(
            self.error,
            field="error",
            maximum=_MAX_ERROR_BYTES,
            allow_empty=True,
        )
        if self.status is AgentWorkerModelExecutionStatus.COMPLETED:
            if self.error or self.error_code:
                raise ValueError("成功的 model turn 不得包含错误。")
        else:
            if self.response or self.tool_calls or not self.error:
                raise ValueError("失败的 model turn 内容无效。")
            _require_identifier(self.error_code, field="error_code")
        _require_int_range(
            self.total_tokens,
            field="total_tokens",
            minimum=0,
            maximum=2**63 - 1,
        )
        _require_int_range(
            self.total_cost_microusd,
            field="total_cost_microusd",
            minimum=0,
            maximum=10**18,
        )
        _validated_model_name(
            self.model,
            field="model",
            allow_empty=self.status is not AgentWorkerModelExecutionStatus.COMPLETED,
        )
        _validated_model_name(
            self.provider_model,
            field="provider_model",
            allow_empty=True,
        )
        if self.provider_response_id_sha256:
            _require_sha256(
                self.provider_response_id_sha256,
                field="provider_response_id_sha256",
            )


def issue_model_execution_preparation(
    *,
    job_id: str,
    request_sha256: str,
    claim_epoch: int,
    model_profile_sha256: str,
    tool_manifest_sha256: str,
) -> AgentWorkerModelExecutionPreparation:
    identity = "\x00".join(
        (
            job_id,
            request_sha256,
            str(claim_epoch),
            model_profile_sha256,
            tool_manifest_sha256,
        )
    ).encode("utf-8")
    return AgentWorkerModelExecutionPreparation(
        execution_id=f"agent-model-{hashlib.sha256(identity).hexdigest()}",
        job_id=job_id,
        request_sha256=request_sha256,
        claim_epoch=claim_epoch,
        model_profile_sha256=model_profile_sha256,
        tool_manifest_sha256=tool_manifest_sha256,
    )


def encode_model_profile(config: ModelConfig) -> bytes:
    """Serialize one exact model profile for encrypted local transport."""
    if not isinstance(config, ModelConfig):
        raise TypeError("config 必须是 ModelConfig。")
    raw = config.model_dump_json().encode("utf-8")
    if not raw or len(raw) > _MAX_MODEL_PROFILE_BYTES:
        raise ValueError("Agent Worker model profile 大小无效。")
    return raw


def decode_model_profile(value: bytes) -> ModelConfig:
    if not isinstance(value, bytes) or not value or len(value) > _MAX_MODEL_PROFILE_BYTES:
        raise ValueError("Agent Worker model profile 大小无效。")
    try:
        payload = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Agent Worker model profile 不是有效 JSON。") from exc
    if not isinstance(payload, dict) or set(payload) != set(ModelConfig.model_fields):
        raise ValueError("Agent Worker model profile 字段集合无效。")
    try:
        return ModelConfig.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent Worker model profile 内容无效。") from exc


def model_profile_sha256(value: bytes) -> str:
    if not isinstance(value, bytes) or not value:
        raise ValueError("Agent Worker model profile 不能为空。")
    return hashlib.sha256(value).hexdigest()


def encode_model_execution_result(
    result: AgentWorkerModelExecutionResult,
) -> bytes:
    if not isinstance(result, AgentWorkerModelExecutionResult):
        raise TypeError("result 必须是 AgentWorkerModelExecutionResult。")
    return json.dumps(
        _json_value(asdict(result)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_model_execution_result(value: bytes) -> AgentWorkerModelExecutionResult:
    if not isinstance(value, bytes) or not value or len(value) > 17 * 1024**2:
        raise ValueError("Agent Worker model result 大小无效。")
    try:
        payload = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Agent Worker model result 不是有效 JSON。") from exc
    if not isinstance(payload, dict) or set(payload) != set(
        AgentWorkerModelExecutionResult.__dataclass_fields__
    ):
        raise ValueError("Agent Worker model result 字段集合无效。")
    try:
        payload["status"] = AgentWorkerModelExecutionStatus(payload["status"])
        return AgentWorkerModelExecutionResult(**payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Agent Worker model result 内容无效。") from exc


async def execute_model_only(
    *,
    preparation: AgentWorkerModelExecutionPreparation,
    request: AgentWorkerRequest,
    payload: AgentJobPayload,
    model_profile: bytes,
) -> AgentWorkerModelExecutionResult:
    """Execute one real provider call without exposing a tool side-effect path."""
    if not isinstance(preparation, AgentWorkerModelExecutionPreparation):
        raise TypeError("preparation 类型无效。")
    if not isinstance(request, AgentWorkerRequest):
        raise TypeError("request 类型无效。")
    if not isinstance(payload, AgentJobPayload):
        raise TypeError("payload 类型无效。")
    if preparation.request_sha256 != request.request_sha256:
        raise ValueError("Agent Worker model execution request fence 不一致。")
    if request.tool_scope:
        raise ValueError("model-only Agent Worker 不接受工具 scope。")
    if model_profile_sha256(model_profile) != preparation.model_profile_sha256:
        raise ValueError("Agent Worker model profile 摘要不一致。")
    config = decode_model_profile(model_profile)
    catalog = None
    if config.catalog_path:
        catalog_path = Path(config.catalog_path).expanduser()
        if not catalog_path.is_absolute():
            raise ValueError("Agent Worker model catalog_path 必须是绝对路径。")
        catalog = load_provider_catalog(catalog_path)
    router = ModelRouter(config, catalog=catalog)
    tier = {
        "fast": ModelTier.FAST,
        "capable": ModelTier.CAPABLE,
        "reasoning": ModelTier.REASONING,
    }[request.model_tier]
    messages: list[dict[str, Any]] = []
    if payload.context:
        messages.append({"role": "system", "content": payload.context})
    messages.append({"role": "user", "content": payload.task})
    timeout_seconds = request.timeout_milliseconds / 1000
    if request.max_cost_microusd == 0:
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="Agent 模型预算为 0，未发起 Provider 请求。",
            error_code="agent_model_budget_zero",
        )
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await router.call(messages=messages, tier=tier, tools=None)
    except TimeoutError:
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.TIMEOUT,
            error="独立 Agent 模型请求超时，未获得可信终态响应。",
            error_code="agent_model_timeout",
            turns=1,
        )
    except Exception:
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="独立 Agent 模型传输失败；详细异常已隔离。",
            error_code="agent_model_transport_failed",
            turns=1,
        )
    try:
        evidence = _normalize_provider_response(response)
    except (TypeError, ValueError):
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="Provider 返回了无法认证的模型响应，内容未发布。",
            error_code="agent_model_response_invalid",
            turns=1,
        )
    if evidence.tool_calls:
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="Provider 返回了未授权工具调用，已拒绝执行。",
            error_code="agent_model_unexpected_tool_call",
            total_tokens=evidence.total_tokens,
            total_cost_microusd=evidence.total_cost_microusd,
            model=evidence.model,
            provider_model=evidence.provider_model,
            provider_response_id_sha256=evidence.provider_response_id_sha256,
            turns=1,
        )
    if len(evidence.response.encode("utf-8")) > MAX_AGENT_MODEL_RESPONSE_BYTES:
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="独立 Agent 模型响应超过安全大小上限，内容未发布。",
            error_code="agent_model_response_too_large",
            total_tokens=evidence.total_tokens,
            total_cost_microusd=evidence.total_cost_microusd,
            model=evidence.model,
            provider_model=evidence.provider_model,
            provider_response_id_sha256=evidence.provider_response_id_sha256,
            turns=1,
        )
    if (
        request.max_cost_microusd is not None
        and evidence.total_cost_microusd > request.max_cost_microusd
    ):
        return _safe_failure(
            preparation,
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="独立 Agent 模型调用已超过本次预算，响应内容未发布。",
            error_code="agent_model_budget_exceeded",
            total_tokens=evidence.total_tokens,
            total_cost_microusd=evidence.total_cost_microusd,
            model=evidence.model,
            provider_model=evidence.provider_model,
            provider_response_id_sha256=evidence.provider_response_id_sha256,
            turns=1,
        )
    return AgentWorkerModelExecutionResult(
        schema_version=1,
        execution_id=preparation.execution_id,
        request_sha256=request.request_sha256,
        status=AgentWorkerModelExecutionStatus.COMPLETED,
        response=evidence.response,
        error="",
        error_code="",
        total_tokens=evidence.total_tokens,
        total_cost_microusd=evidence.total_cost_microusd,
        turns=1,
        model=evidence.model,
        provider_model=evidence.provider_model,
        provider_response_id_sha256=evidence.provider_response_id_sha256,
    )


async def execute_model_turn(
    *,
    preparation: AgentWorkerModelExecutionPreparation,
    request: AgentWorkerRequest,
    model_profile: bytes,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    timeout_milliseconds: int,
) -> AgentWorkerModelTurnResult:
    """Execute one bounded provider turn for the child-owned Agent loop."""
    if not isinstance(preparation, AgentWorkerModelExecutionPreparation):
        raise TypeError("preparation 类型无效。")
    if not isinstance(request, AgentWorkerRequest):
        raise TypeError("request 类型无效。")
    if preparation.request_sha256 != request.request_sha256:
        raise ValueError("Agent Worker model turn request fence 不一致。")
    if model_profile_sha256(model_profile) != preparation.model_profile_sha256:
        raise ValueError("Agent Worker model profile 摘要不一致。")
    try:
        _validate_model_messages(messages)
        _validate_model_tools(tools)
    except (TypeError, ValueError):
        return _safe_turn_failure(
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="独立 Agent 上下文或工具 schema 超过安全边界，未发起 Provider 请求。",
            error_code="agent_model_input_invalid",
        )
    _require_int_range(
        timeout_milliseconds,
        field="timeout_milliseconds",
        minimum=1,
        maximum=request.timeout_milliseconds,
    )
    if request.max_cost_microusd == 0:
        return _safe_turn_failure(
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="Agent 模型预算为 0，未发起 Provider 请求。",
            error_code="agent_model_budget_zero",
        )
    config = decode_model_profile(model_profile)
    catalog = None
    if config.catalog_path:
        catalog_path = Path(config.catalog_path).expanduser()
        if not catalog_path.is_absolute():
            raise ValueError("Agent Worker model catalog_path 必须是绝对路径。")
        catalog = load_provider_catalog(catalog_path)
    router = ModelRouter(config, catalog=catalog)
    tier = {
        "fast": ModelTier.FAST,
        "capable": ModelTier.CAPABLE,
        "reasoning": ModelTier.REASONING,
    }[request.model_tier]
    try:
        async with asyncio.timeout(timeout_milliseconds / 1000):
            response = await router.call(messages=messages, tier=tier, tools=tools)
    except TimeoutError:
        return _safe_turn_failure(
            status=AgentWorkerModelExecutionStatus.TIMEOUT,
            error="独立 Agent 模型请求超时，未获得可信终态响应。",
            error_code="agent_model_timeout",
        )
    except Exception:
        return _safe_turn_failure(
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="独立 Agent 模型传输失败；详细异常已隔离。",
            error_code="agent_model_transport_failed",
        )
    try:
        evidence = _normalize_provider_response(response)
    except (TypeError, ValueError):
        return _safe_turn_failure(
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="Provider 返回了无法认证的模型响应，内容未发布。",
            error_code="agent_model_response_invalid",
        )
    if len(evidence.response.encode("utf-8")) > MAX_AGENT_MODEL_RESPONSE_BYTES:
        return _safe_turn_failure(
            status=AgentWorkerModelExecutionStatus.ERROR,
            error="独立 Agent 模型响应超过安全大小上限，内容未发布。",
            error_code="agent_model_response_too_large",
            evidence=evidence,
        )
    return AgentWorkerModelTurnResult(
        status=AgentWorkerModelExecutionStatus.COMPLETED,
        response=evidence.response,
        tool_calls=evidence.tool_calls,
        error="",
        error_code="",
        total_tokens=evidence.total_tokens,
        total_cost_microusd=evidence.total_cost_microusd,
        model=evidence.model,
        provider_model=evidence.provider_model,
        provider_response_id_sha256=evidence.provider_response_id_sha256,
    )


@dataclass(frozen=True, slots=True)
class _NormalizedProviderResponse:
    response: str
    tool_calls: tuple[dict[str, Any], ...]
    total_tokens: int
    total_cost_microusd: int
    model: str
    provider_model: str
    provider_response_id_sha256: str


def _safe_turn_failure(
    *,
    status: AgentWorkerModelExecutionStatus,
    error: str,
    error_code: str,
    evidence: _NormalizedProviderResponse | None = None,
) -> AgentWorkerModelTurnResult:
    return AgentWorkerModelTurnResult(
        status=status,
        response="",
        tool_calls=(),
        error=error,
        error_code=error_code,
        total_tokens=evidence.total_tokens if evidence is not None else 0,
        total_cost_microusd=(
            evidence.total_cost_microusd if evidence is not None else 0
        ),
        model=evidence.model if evidence is not None else "",
        provider_model=evidence.provider_model if evidence is not None else "",
        provider_response_id_sha256=(
            evidence.provider_response_id_sha256 if evidence is not None else ""
        ),
    )


def _normalize_provider_response(response: ModelResponse) -> _NormalizedProviderResponse:
    """Validate all untrusted transport-derived fields before durable use."""
    if not isinstance(response, ModelResponse):
        raise TypeError("Provider response 类型无效。")
    content = response.content
    if not isinstance(content, str):
        raise TypeError("Provider response content 类型无效。")
    if not isinstance(response.tool_calls, list) or any(
        not isinstance(item, dict) for item in response.tool_calls
    ):
        raise TypeError("Provider tool_calls 类型无效。")
    _bounded_json_bytes(
        response.tool_calls,
        maximum=MAX_AGENT_MODEL_TOOLS_BYTES,
        label="provider tool_calls",
        allow_empty=True,
    )
    _require_int_range(
        response.usage.total_tokens,
        field="total_tokens",
        minimum=0,
        maximum=2**63 - 1,
    )
    model = _validated_model_name(response.model, field="model", allow_empty=False)
    provider_model = _validated_model_name(
        response.provider_model,
        field="provider_model",
        allow_empty=True,
    )
    digest = response.call_evidence.provider_response_id_sha256
    if digest:
        _require_sha256(digest, field="provider_response_id_sha256")
    return _NormalizedProviderResponse(
        response=content,
        tool_calls=tuple(response.tool_calls),
        total_tokens=response.usage.total_tokens,
        total_cost_microusd=_cost_to_microusd(response.usage.cost_usd),
        model=model,
        provider_model=provider_model,
        provider_response_id_sha256=digest,
    )


def _safe_failure(
    preparation: AgentWorkerModelExecutionPreparation,
    *,
    status: AgentWorkerModelExecutionStatus,
    error: str,
    error_code: str,
    total_tokens: int = 0,
    total_cost_microusd: int = 0,
    model: str = "",
    provider_model: str = "",
    provider_response_id_sha256: str = "",
    turns: int = 0,
) -> AgentWorkerModelExecutionResult:
    return AgentWorkerModelExecutionResult(
        schema_version=1,
        execution_id=preparation.execution_id,
        request_sha256=preparation.request_sha256,
        status=status,
        response="",
        error=error,
        error_code=error_code,
        total_tokens=total_tokens,
        total_cost_microusd=total_cost_microusd,
        turns=turns,
        model=model,
        provider_model=provider_model,
        provider_response_id_sha256=provider_response_id_sha256,
    )


def _cost_to_microusd(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("模型成本必须是数值。")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("模型成本必须是有限非负数。")
    microusd = round(number * 1_000_000)
    if microusd > 10**18:
        raise ValueError("模型成本超过安全范围。")
    return microusd


def _validated_model_name(value: str, *, field: str, allow_empty: bool) -> str:
    _require_bounded_text(
        value,
        field=field,
        maximum=_MAX_MODEL_NAME_BYTES,
        allow_empty=allow_empty,
    )
    return value


def _validate_model_messages(messages: list[dict[str, Any]]) -> None:
    if not isinstance(messages, list) or not 1 <= len(messages) <= 2_100:
        raise ValueError("Agent Worker model messages 数量无效。")
    if any(not isinstance(item, dict) for item in messages):
        raise TypeError("Agent Worker model message 必须是对象。")
    roles = {str(item.get("role", "")) for item in messages}
    if not roles.issubset({"system", "user", "assistant", "tool"}):
        raise ValueError("Agent Worker model message role 无效。")
    _bounded_json_bytes(
        messages,
        maximum=MAX_AGENT_MODEL_MESSAGES_BYTES,
        label="model messages",
    )


def _validate_model_tools(tools: list[dict[str, Any]] | None) -> None:
    if tools is None:
        return
    if not isinstance(tools, list) or not tools:
        raise ValueError("Agent Worker model tools 必须是非空数组或 None。")
    if any(not isinstance(item, dict) for item in tools):
        raise TypeError("Agent Worker model tool schema 必须是对象。")
    _bounded_json_bytes(
        tools,
        maximum=MAX_AGENT_MODEL_TOOLS_BYTES,
        label="model tools",
    )


def _bounded_json_bytes(
    value: Any,
    *,
    maximum: int,
    label: str,
    allow_empty: bool = False,
) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Agent Worker {label} JSON 无效。") from exc
    if (not allow_empty and not encoded) or len(encoded) > maximum:
        raise ValueError(f"Agent Worker {label} 大小无效。")
    return encoded


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _require_identifier(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} 格式无效。")


def _require_sha256(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} 必须是小写 SHA-256。")


def _require_positive_int(value: int, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数。")


def _require_int_range(
    value: int,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间。")


def _require_bounded_text(
    value: str,
    *,
    field: str,
    maximum: int,
    allow_empty: bool,
) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} 必须是字符串。")
    size = len(value.encode("utf-8"))
    if (not allow_empty and not value) or size > maximum:
        raise ValueError(f"{field} 大小无效。")


__all__ = [
    "AgentWorkerModelExecutionPreparation",
    "AgentWorkerModelExecutionResult",
    "AgentWorkerModelExecutionStatus",
    "AgentWorkerModelTurnResult",
    "MAX_AGENT_MODEL_RESPONSE_BYTES",
    "execute_model_turn",
    "decode_model_execution_result",
    "decode_model_profile",
    "encode_model_execution_result",
    "encode_model_profile",
    "execute_model_only",
    "issue_model_execution_preparation",
    "model_profile_sha256",
]
