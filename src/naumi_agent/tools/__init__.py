"""NaumiAgent 工具系统."""

from typing import TYPE_CHECKING, Any

from naumi_agent.tools.base import (
    InterruptBehavior,
    Tool,
    ToolCall,
    ToolMetadata,
    ToolRegistry,
    ToolResult,
    ToolSchema,
)
from naumi_agent.tools.builtin import BashRunTool, FileEditTool, FileReadTool, FileWriteTool

if TYPE_CHECKING:
    from naumi_agent.tools.search import ToolSearchTool


def __getattr__(name: str) -> Any:
    if name == "ToolSearchTool":
        from naumi_agent.tools.search import ToolSearchTool

        return ToolSearchTool
    raise AttributeError(name)

__all__ = [
    "Tool",
    "ToolCall",
    "ToolMetadata",
    "ToolResult",
    "ToolSchema",
    "ToolRegistry",
    "InterruptBehavior",
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "BashRunTool",
    "ToolSearchTool",
]
