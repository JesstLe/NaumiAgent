"""Bounded encrypted Tool RPC payload contracts for an Agent Worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from naumi_agent.tools.base import ToolCall, ToolResult

MAX_AGENT_TOOL_MANIFEST_BYTES = 4 * 1024**2
MAX_AGENT_TOOL_ARGUMENT_BYTES = 2 * 1024**2
MAX_AGENT_TOOL_RESULT_BYTES = 16 * 1024**2
MAX_AGENT_TOOL_CALL_BATCH_BYTES = 16 * 1024**2
MAX_AGENT_TOOL_RESULT_BATCH_BYTES = 32 * 1024**2
MAX_AGENT_TOOL_CALLS_PER_TURN = 64

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESULT_STATUSES = frozenset({"success", "error", "skipped", "aborted"})


@dataclass(frozen=True, slots=True)
class AgentWorkerToolManifest:
    schema_version: int
    tool_scope: tuple[str, ...]
    tools: tuple[dict[str, Any], ...] = field(repr=False)
    manifest_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Agent Worker tool manifest schema_version 必须为 1。")
        _validate_tool_scope(self.tool_scope)
        normalized = _normalize_tool_schemas(self.tools)
        if normalized != self.tools:
            raise ValueError("Agent Worker tool manifest schema 未规范化。")
        names = tuple(_tool_schema_name(item) for item in self.tools)
        if names != self.tool_scope:
            raise ValueError("Agent Worker tool manifest 与 request scope 不一致。")
        _require_sha256(self.manifest_sha256, field_name="manifest_sha256")
        expected = _manifest_digest(self.tool_scope, self.tools)
        if not hmac.compare_digest(self.manifest_sha256, expected):
            raise ValueError("Agent Worker tool manifest 摘要无效。")


@dataclass(frozen=True, slots=True)
class AgentWorkerToolCallBatch:
    schema_version: int
    execution_id: str
    turn: int
    calls: tuple[ToolCall, ...] = field(repr=False)
    batch_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Agent Worker tool call batch schema_version 必须为 1。")
        _require_identifier(self.execution_id, field_name="execution_id")
        _require_int_range(
            self.turn,
            field_name="turn",
            minimum=1,
            maximum=1_000,
        )
        if not 1 <= len(self.calls) <= MAX_AGENT_TOOL_CALLS_PER_TURN:
            raise ValueError("Agent Worker 单轮工具调用数量无效。")
        seen: set[str] = set()
        seen_signatures: set[tuple[str, str]] = set()
        for call in self.calls:
            _validate_tool_call(call)
            if call.id in seen:
                raise ValueError("Agent Worker 同轮工具 call_id 不能重复。")
            seen.add(call.id)
            signature = (call.name, _canonical_arguments(call.arguments))
            if signature in seen_signatures:
                raise ValueError("Agent Worker 同轮工具调用内容不能重复。")
            seen_signatures.add(signature)
        _require_sha256(self.batch_sha256, field_name="batch_sha256")
        expected = _tool_call_batch_digest(
            execution_id=self.execution_id,
            turn=self.turn,
            calls=self.calls,
        )
        if not hmac.compare_digest(self.batch_sha256, expected):
            raise ValueError("Agent Worker tool call batch 摘要无效。")


@dataclass(frozen=True, slots=True)
class AgentWorkerToolResultBatch:
    schema_version: int
    execution_id: str
    turn: int
    call_batch_sha256: str
    results: tuple[ToolResult, ...] = field(repr=False)
    batch_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Agent Worker tool result batch schema_version 必须为 1。")
        _require_identifier(self.execution_id, field_name="execution_id")
        _require_int_range(
            self.turn,
            field_name="turn",
            minimum=1,
            maximum=1_000,
        )
        _require_sha256(
            self.call_batch_sha256,
            field_name="call_batch_sha256",
        )
        if not 1 <= len(self.results) <= MAX_AGENT_TOOL_CALLS_PER_TURN:
            raise ValueError("Agent Worker 单轮工具结果数量无效。")
        seen: set[str] = set()
        for result in self.results:
            _validate_tool_result(result)
            if result.call_id in seen:
                raise ValueError("Agent Worker 同轮工具结果 call_id 不能重复。")
            seen.add(result.call_id)
        _require_sha256(self.batch_sha256, field_name="batch_sha256")
        expected = _tool_result_batch_digest(
            execution_id=self.execution_id,
            turn=self.turn,
            call_batch_sha256=self.call_batch_sha256,
            results=self.results,
        )
        if not hmac.compare_digest(self.batch_sha256, expected):
            raise ValueError("Agent Worker tool result batch 摘要无效。")


def issue_tool_manifest(
    *,
    tool_scope: tuple[str, ...],
    tools: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> AgentWorkerToolManifest:
    normalized_scope = tuple(tool_scope)
    _validate_tool_scope(normalized_scope)
    normalized_tools = _normalize_tool_schemas(tools)
    names = tuple(_tool_schema_name(item) for item in normalized_tools)
    if names != normalized_scope:
        raise ValueError("工具 schema 必须按 request tool_scope 精确、完整排序。")
    return AgentWorkerToolManifest(
        schema_version=1,
        tool_scope=normalized_scope,
        tools=normalized_tools,
        manifest_sha256=_manifest_digest(normalized_scope, normalized_tools),
    )


def issue_tool_call_batch(
    *,
    execution_id: str,
    turn: int,
    raw_calls: list[dict[str, Any]],
    tool_scope: tuple[str, ...],
) -> AgentWorkerToolCallBatch:
    _validate_tool_scope(tool_scope)
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ValueError("Provider tool_calls 必须是非空数组。")
    if len(raw_calls) > MAX_AGENT_TOOL_CALLS_PER_TURN:
        raise ValueError("Provider 单轮工具调用超过安全上限。")
    calls = tuple(_parse_provider_tool_call(item) for item in raw_calls)
    allowed = set(tool_scope)
    for call in calls:
        if call.name not in allowed:
            raise ValueError("Provider 请求了 Agent Worker scope 外工具。")
    return AgentWorkerToolCallBatch(
        schema_version=1,
        execution_id=execution_id,
        turn=turn,
        calls=calls,
        batch_sha256=_tool_call_batch_digest(
            execution_id=execution_id,
            turn=turn,
            calls=calls,
        ),
    )


def issue_tool_result_batch(
    *,
    call_batch: AgentWorkerToolCallBatch,
    results: list[ToolResult] | tuple[ToolResult, ...],
) -> AgentWorkerToolResultBatch:
    if not isinstance(call_batch, AgentWorkerToolCallBatch):
        raise TypeError("call_batch 类型无效。")
    normalized = tuple(results)
    if tuple(result.call_id for result in normalized) != tuple(
        call.id for call in call_batch.calls
    ):
        raise ValueError("Agent Worker tool result 顺序或 call_id 不一致。")
    return AgentWorkerToolResultBatch(
        schema_version=1,
        execution_id=call_batch.execution_id,
        turn=call_batch.turn,
        call_batch_sha256=call_batch.batch_sha256,
        results=normalized,
        batch_sha256=_tool_result_batch_digest(
            execution_id=call_batch.execution_id,
            turn=call_batch.turn,
            call_batch_sha256=call_batch.batch_sha256,
            results=normalized,
        ),
    )


def tool_call_signature(call: ToolCall) -> str:
    """Return a stable signature that ignores provider-assigned call IDs."""
    _validate_tool_call(call)
    return _digest(
        {
            "name": call.name,
            "arguments": json.loads(call.arguments),
        }
    )


def encode_tool_manifest(manifest: AgentWorkerToolManifest) -> bytes:
    if not isinstance(manifest, AgentWorkerToolManifest):
        raise TypeError("manifest 类型无效。")
    AgentWorkerToolManifest(
        schema_version=manifest.schema_version,
        tool_scope=manifest.tool_scope,
        tools=manifest.tools,
        manifest_sha256=manifest.manifest_sha256,
    )
    return _bounded_json_bytes(
        {
            "schema_version": manifest.schema_version,
            "tool_scope": list(manifest.tool_scope),
            "tools": list(manifest.tools),
            "manifest_sha256": manifest.manifest_sha256,
        },
        maximum=MAX_AGENT_TOOL_MANIFEST_BYTES,
        label="tool manifest",
    )


def decode_tool_manifest(value: bytes) -> AgentWorkerToolManifest:
    payload = _decode_exact_json(
        value,
        fields={"schema_version", "tool_scope", "tools", "manifest_sha256"},
        maximum=MAX_AGENT_TOOL_MANIFEST_BYTES,
        label="tool manifest",
    )
    try:
        return AgentWorkerToolManifest(
            schema_version=payload["schema_version"],
            tool_scope=tuple(payload["tool_scope"]),
            tools=tuple(payload["tools"]),
            manifest_sha256=payload["manifest_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Agent Worker tool manifest 内容无效。") from exc


def encode_tool_call_batch(batch: AgentWorkerToolCallBatch) -> bytes:
    if not isinstance(batch, AgentWorkerToolCallBatch):
        raise TypeError("batch 类型无效。")
    return _bounded_json_bytes(
        {
            "schema_version": batch.schema_version,
            "execution_id": batch.execution_id,
            "turn": batch.turn,
            "calls": [asdict(call) for call in batch.calls],
            "batch_sha256": batch.batch_sha256,
        },
        maximum=MAX_AGENT_TOOL_CALL_BATCH_BYTES,
        label="tool call batch",
    )


def decode_tool_call_batch(value: bytes) -> AgentWorkerToolCallBatch:
    payload = _decode_exact_json(
        value,
        fields={"schema_version", "execution_id", "turn", "calls", "batch_sha256"},
        maximum=MAX_AGENT_TOOL_CALL_BATCH_BYTES,
        label="tool call batch",
    )
    try:
        calls = tuple(ToolCall(**item) for item in payload["calls"])
        return AgentWorkerToolCallBatch(
            schema_version=payload["schema_version"],
            execution_id=payload["execution_id"],
            turn=payload["turn"],
            calls=calls,
            batch_sha256=payload["batch_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Agent Worker tool call batch 内容无效。") from exc


def encode_tool_result_batch(batch: AgentWorkerToolResultBatch) -> bytes:
    if not isinstance(batch, AgentWorkerToolResultBatch):
        raise TypeError("batch 类型无效。")
    return _bounded_json_bytes(
        {
            "schema_version": batch.schema_version,
            "execution_id": batch.execution_id,
            "turn": batch.turn,
            "call_batch_sha256": batch.call_batch_sha256,
            "results": [asdict(result) for result in batch.results],
            "batch_sha256": batch.batch_sha256,
        },
        maximum=MAX_AGENT_TOOL_RESULT_BATCH_BYTES,
        label="tool result batch",
    )


def decode_tool_result_batch(value: bytes) -> AgentWorkerToolResultBatch:
    payload = _decode_exact_json(
        value,
        fields={
            "schema_version",
            "execution_id",
            "turn",
            "call_batch_sha256",
            "results",
            "batch_sha256",
        },
        maximum=MAX_AGENT_TOOL_RESULT_BATCH_BYTES,
        label="tool result batch",
    )
    try:
        results = tuple(ToolResult(**item) for item in payload["results"])
        return AgentWorkerToolResultBatch(
            schema_version=payload["schema_version"],
            execution_id=payload["execution_id"],
            turn=payload["turn"],
            call_batch_sha256=payload["call_batch_sha256"],
            results=results,
            batch_sha256=payload["batch_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Agent Worker tool result batch 内容无效。") from exc


def _normalize_tool_schemas(
    tools: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    if not isinstance(tools, (list, tuple)):
        raise TypeError("tools 必须是数组。")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tools:
        if not isinstance(item, dict) or set(item) != {"type", "function"}:
            raise ValueError("工具 schema 顶层字段无效。")
        if item.get("type") != "function" or not isinstance(item.get("function"), dict):
            raise ValueError("工具 schema 必须是 function。")
        function = item["function"]
        if set(function) != {"name", "description", "parameters"}:
            raise ValueError("工具 function schema 字段无效。")
        name = function["name"]
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            raise ValueError("工具 schema name 格式无效。")
        if name in seen:
            raise ValueError("工具 schema name 不能重复。")
        seen.add(name)
        description = function["description"]
        if not isinstance(description, str) or len(description.encode("utf-8")) > 64 * 1024:
            raise ValueError("工具 schema description 大小无效。")
        parameters = function["parameters"]
        if not isinstance(parameters, dict):
            raise ValueError("工具 schema parameters 必须是对象。")
        copied = json.loads(_canonical_json(item))
        normalized.append(copied)
    _bounded_json_bytes(
        normalized,
        maximum=MAX_AGENT_TOOL_MANIFEST_BYTES,
        label="tool manifest",
    )
    return tuple(normalized)


def _parse_provider_tool_call(value: dict[str, Any]) -> ToolCall:
    if not isinstance(value, dict) or set(value) != {"id", "type", "function"}:
        raise ValueError("Provider tool call 字段集合无效。")
    if value.get("type") != "function" or not isinstance(value.get("function"), dict):
        raise ValueError("Provider tool call 类型无效。")
    function = value["function"]
    if set(function) != {"name", "arguments"}:
        raise ValueError("Provider tool call function 字段集合无效。")
    call = ToolCall(
        id=function_call_id(value["id"]),
        name=function_tool_name(function["name"]),
        arguments=function_arguments(function["arguments"]),
    )
    _validate_tool_call(call)
    return call


def function_call_id(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("Provider tool call id 格式无效。")
    return value


def function_tool_name(value: object) -> str:
    if not isinstance(value, str) or not _TOOL_NAME_RE.fullmatch(value):
        raise ValueError("Provider tool name 格式无效。")
    return value


def function_arguments(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_AGENT_TOOL_ARGUMENT_BYTES:
        raise ValueError("Provider tool arguments 大小无效。")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Provider tool arguments 不是有效 JSON。") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Provider tool arguments 必须是 JSON object。")
    return value


def _canonical_arguments(value: str) -> str:
    return _canonical_json(json.loads(value))


def _validate_tool_call(call: ToolCall) -> None:
    if not isinstance(call, ToolCall):
        raise TypeError("Agent Worker tool call 类型无效。")
    function_call_id(call.id)
    function_tool_name(call.name)
    function_arguments(call.arguments)


def _validate_tool_result(result: ToolResult) -> None:
    if not isinstance(result, ToolResult):
        raise TypeError("Agent Worker tool result 类型无效。")
    function_call_id(result.call_id)
    if result.status not in _RESULT_STATUSES:
        raise ValueError("Agent Worker tool result status 无效。")
    if (
        not isinstance(result.content, str)
        or len(result.content.encode("utf-8")) > MAX_AGENT_TOOL_RESULT_BYTES
    ):
        raise ValueError("Agent Worker tool result content 大小无效。")
    _require_int_range(
        result.duration_ms,
        field_name="duration_ms",
        minimum=0,
        maximum=7 * 24 * 60 * 60 * 1000,
    )


def _validate_tool_scope(tool_scope: tuple[str, ...]) -> None:
    if not isinstance(tool_scope, tuple):
        raise TypeError("tool_scope 必须是 tuple。")
    if len(tool_scope) > 1_000:
        raise ValueError("tool_scope 超过安全上限。")
    if tuple(sorted(set(tool_scope))) != tool_scope:
        raise ValueError("tool_scope 必须去重并排序。")
    for name in tool_scope:
        function_tool_name(name)


def _tool_schema_name(value: dict[str, Any]) -> str:
    return str(value["function"]["name"])


def _manifest_digest(
    tool_scope: tuple[str, ...],
    tools: tuple[dict[str, Any], ...],
) -> str:
    return _digest({"schema_version": 1, "tool_scope": tool_scope, "tools": tools})


def _tool_call_batch_digest(
    *,
    execution_id: str,
    turn: int,
    calls: tuple[ToolCall, ...],
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "execution_id": execution_id,
            "turn": turn,
            "calls": [asdict(call) for call in calls],
        }
    )


def _tool_result_batch_digest(
    *,
    execution_id: str,
    turn: int,
    call_batch_sha256: str,
    results: tuple[ToolResult, ...],
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "execution_id": execution_id,
            "turn": turn,
            "call_batch_sha256": call_batch_sha256,
            "results": [asdict(result) for result in results],
        }
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent Worker Tool RPC JSON 内容无效。") from exc


def _bounded_json_bytes(value: Any, *, maximum: int, label: str) -> bytes:
    encoded = _canonical_json(value).encode("utf-8")
    if not encoded or len(encoded) > maximum:
        raise ValueError(f"Agent Worker {label} 大小无效。")
    return encoded


def _decode_exact_json(
    value: bytes,
    *,
    fields: set[str],
    maximum: int,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, bytes) or not value or len(value) > maximum:
        raise ValueError(f"Agent Worker {label} 大小无效。")
    try:
        payload = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Agent Worker {label} 不是有效 JSON。") from exc
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError(f"Agent Worker {label} 字段集合无效。")
    return payload


def _require_identifier(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} 格式无效。")


def _require_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} 必须是小写 SHA-256。")


def _require_int_range(
    value: int,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field_name} 必须在 {minimum} 到 {maximum} 之间。")


__all__ = [
    "AgentWorkerToolCallBatch",
    "AgentWorkerToolManifest",
    "AgentWorkerToolResultBatch",
    "MAX_AGENT_TOOL_ARGUMENT_BYTES",
    "MAX_AGENT_TOOL_CALL_BATCH_BYTES",
    "MAX_AGENT_TOOL_RESULT_BATCH_BYTES",
    "MAX_AGENT_TOOL_CALLS_PER_TURN",
    "MAX_AGENT_TOOL_MANIFEST_BYTES",
    "MAX_AGENT_TOOL_RESULT_BYTES",
    "decode_tool_call_batch",
    "decode_tool_manifest",
    "decode_tool_result_batch",
    "encode_tool_call_batch",
    "encode_tool_manifest",
    "encode_tool_result_batch",
    "issue_tool_call_batch",
    "issue_tool_manifest",
    "issue_tool_result_batch",
    "tool_call_signature",
]
