"""工具基类与注册表."""

from __future__ import annotations

import json
import logging
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_RE = re.compile(
    r"(?:\b(?:api[_-]?key|password|secret|token|authorization|cookie)\b\s*[:=]\s*\S+)"
    r"|(?:\bbearer\s+\S+)|(?:\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


class ToolExecutionError(RuntimeError):
    """A declared, user-safe failure raised by a Tool implementation."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if not isinstance(code, str) or _TOOL_ERROR_CODE_RE.fullmatch(code) is None:
            raise ValueError("工具错误码必须是最长 64 字符的小写标识符")
        if not isinstance(message, str):
            raise TypeError("工具错误消息必须是字符串")
        if (
            not message
            or message != message.strip()
            or len(message) > 300
            or any(ord(character) < 32 or ord(character) == 127 for character in message)
            or _SECRET_RE.search(message)
        ):
            raise ValueError("工具错误消息必须简洁、无控制字符且不包含疑似凭据")
        if not isinstance(retryable, bool):
            raise TypeError("工具错误 retryable 必须是布尔值")
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class ToolSchema:
    """工具的 JSON Schema 描述（用于传给 LLM）."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """LLM 发起的工具调用."""

    id: str
    name: str
    arguments: str  # JSON string


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果."""

    call_id: str
    status: str  # "success" | "error"
    content: str
    duration_ms: int = 0
    error_code: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.error_code and _TOOL_ERROR_CODE_RE.fullmatch(self.error_code) is None:
            raise ValueError("ToolResult.error_code 格式无效")
        if not isinstance(self.retryable, bool):
            raise TypeError("ToolResult.retryable 必须是布尔值")
        if self.status != "error" and (self.error_code or self.retryable):
            raise ValueError("只有 error ToolResult 可以携带结构化错误字段")
        if self.retryable and not self.error_code:
            raise ValueError("retryable ToolResult 必须携带 error_code")


class InterruptBehavior(StrEnum):
    CANCEL = "cancel"
    BLOCK = "block"


@dataclass(frozen=True)
class ToolMetadata:
    """工具执行与权限系统使用的能力元数据."""

    read_only: bool = False
    destructive: bool = False
    concurrency_safe: bool = False
    requires_confirmation: bool | None = None
    path_argument_names: tuple[str, ...] = ("path", "cwd")
    command_argument_names: tuple[str, ...] = ("command",)
    interrupt_behavior: InterruptBehavior = InterruptBehavior.BLOCK
    user_facing_name: str | None = None
    search_hint: str = ""
    delegated_tool_names: tuple[str, ...] = ()
    requires_persistent_authorization: bool = False


class Tool(ABC):
    """所有工具的基类."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.parameters_schema,
        )

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]: ...

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(user_facing_name=self.name)

    @property
    def is_read_only(self) -> bool:
        return self.metadata.read_only

    @property
    def is_destructive(self) -> bool:
        return self.metadata.destructive

    @property
    def is_concurrency_safe(self) -> bool:
        return self.metadata.concurrency_safe

    @property
    def user_facing_name(self) -> str:
        return self.metadata.user_facing_name or self.name

    def to_openai_tool(self) -> dict[str, Any]:
        """转为 OpenAI function calling 格式."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """执行工具，返回结果文本."""
        ...

    def parse_arguments(self, raw: Any) -> dict[str, Any]:
        """Parse tool arguments from provider JSON strings or decoded objects."""
        if isinstance(raw, dict):
            return dict(raw)

        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as e:
            raise ValueError(f"Invalid JSON arguments for {self.name}: {e}") from e

        if not isinstance(parsed, dict):
            raise ValueError(
                f"Invalid JSON arguments for {self.name}: expected object, "
                f"got {type(parsed).__name__}"
            )
        return parsed


class ToolRegistryConflictError(RuntimeError):
    """Raised when a guarded registry mutation would replace another Tool."""


class ToolRegistry:
    """工具注册表."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()

    def register(self, tool: Tool) -> None:
        """Register a trusted built-in, preserving the legacy replace behavior."""
        name = _validated_tool_registration_name(tool)
        with self._lock:
            self._tools[name] = tool

    def register_unique(self, tool: Tool) -> None:
        """Register one untrusted/dynamic Tool without any exact or alias overwrite."""
        name = _validated_tool_registration_name(tool)
        with self._lock:
            conflicts = self._conflicts_locked(name)
            if conflicts:
                joined = "、".join(f"`{name}`" for name in conflicts)
                raise ToolRegistryConflictError(
                    f"工具 `{name}` 与现有注册项 {joined} 冲突，未修改注册表。"
                )
            self._tools[name] = tool

    def unregister_if_same(self, name: str, expected_tool: Tool) -> bool:
        """Remove only the exact instance installed by the caller."""
        normalized = _validate_tool_name(name)
        if not isinstance(expected_tool, Tool):
            raise TypeError("expected_tool 必须是 Tool 实例")
        with self._lock:
            current = self._tools.get(normalized)
            if current is not expected_tool:
                return False
            del self._tools[normalized]
            return True

    def get_exact(self, name: str) -> Tool | None:
        """Resolve an exact public name without legacy namespace fallback."""
        if not isinstance(name, str):
            return None
        with self._lock:
            return self._tools.get(name)

    def conflicts_for(self, name: str) -> tuple[str, ...]:
        """Return stable exact/legacy-alias conflicts without mutation."""
        normalized = _validate_tool_name(name)
        with self._lock:
            return self._conflicts_locked(normalized)

    def _conflicts_locked(self, name: str) -> tuple[str, ...]:
        alias = _legacy_tool_alias(name)
        conflicts = [
            registered
            for registered in self._tools
            if registered == name
            or _legacy_tool_alias(registered) == alias
        ]
        return tuple(sorted(conflicts))

    def get(self, name: str) -> Tool | None:
        with self._lock:
            if name in self._tools:
                return self._tools[name]
            # 某些 API（如 Kimi）返回的工具名可能带 namespace 前缀，
            # 例如 "default.web_search" 或 "default__web_search"
            normalized = _legacy_tool_alias(name)
            return self._tools.get(normalized)

    def all(self) -> list[Tool]:
        with self._lock:
            return list(self._tools.values())

    def get_openai_tools(self) -> list[dict[str, Any]]:
        return [tool.to_openai_tool() for tool in self.all()]

    @property
    def names(self) -> list[str]:
        with self._lock:
            return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)


def _validated_tool_registration_name(tool: Tool) -> str:
    if not isinstance(tool, Tool):
        raise TypeError("只能注册 Tool 实例")
    return _validate_tool_name(tool.name)


def _validate_tool_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 128
        or any(ord(character) < 33 or ord(character) == 127 for character in name)
    ):
        raise ValueError("工具名必须是 1..128 字符且不含空白或控制字符")
    return name


def _legacy_tool_alias(name: str) -> str:
    normalized = name
    if "." in normalized:
        normalized = normalized.split(".")[-1]
    elif "__" in normalized:
        normalized = normalized.split("__")[-1]
    return normalized
