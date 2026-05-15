"""工具基类与注册表."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


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

    def parse_arguments(self, raw: str) -> dict[str, Any]:
        """解析 JSON 参数字符串."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON arguments for {self.name}: {e}") from e


class ToolRegistry:
    """工具注册表."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        if name in self._tools:
            return self._tools[name]
        # 某些 API（如 Kimi）返回的工具名可能带 namespace 前缀，
        # 例如 "default.web_search" 或 "default__web_search"
        normalized = name
        if "." in normalized:
            normalized = normalized.split(".")[-1]
        elif "__" in normalized:
            normalized = normalized.split("__")[-1]
        return self._tools.get(normalized)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def get_openai_tools(self) -> list[dict[str, Any]]:
        return [t.to_openai_tool() for t in self._tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
