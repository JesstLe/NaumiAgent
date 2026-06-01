"""Vibe demo scaffold generation tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from naumi_agent.tools.analysis_support.vibe import (
    build_vibe_scaffold,
    format_vibe_scaffold,
    scan_vibe_request,
    write_vibe_scaffold,
)
from naumi_agent.tools.base import Tool, ToolMetadata

RouterGetter = Callable[[], Any]
RunAnalysis = Callable[[Any, str, str], Awaitable[str]]

VIBE_SYSTEM = """\
You are in VIBE MODE. Drop all architectural concerns, edge cases, and perfectionism.

RULES:
- Output the FASTEST possible working code
- No error handling unless it is a single line
- No comments unless critical
- Use the most lightweight libraries available
- Hardcode configuration values — refinement comes later
- Skip writing tests initially
- If there is a 3-line solution and a 30-line "proper" solution, use the 3-line one
- Output COMPLETE, RUNNABLE code — no TODOs, no gaps, no scaffolding

Focus on the CORE functionality. Ship it.
"""


class VibeModeTool(Tool):
    """Generate a runnable demo scaffold with optional LLM enhancement."""

    def __init__(
        self,
        *,
        router_getter: RouterGetter | None = None,
        run_analysis: RunAnalysis | None = None,
    ) -> None:
        self._router_getter = router_getter or (lambda: None)
        self._run_analysis = run_analysis

    @property
    def name(self) -> str:
        return "analysis_vibe"

    @property
    def description(self) -> str:
        return (
            "极速构建模式：根据需求生成能直接运行的最小 Demo scaffold，"
            "可选写入 output_dir，并在模型可用时追加 LLM 增强建议。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            destructive=True,
            path_argument_names=("output_dir",),
            user_facing_name="极速构建 Demo",
            search_hint="rapid prototype scaffold runnable demo files",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "要构建的功能描述",
                },
                "tech_stack": {
                    "type": "string",
                    "description": "技术栈偏好（如 Python/Flask, Node.js/Express）",
                    "default": "",
                },
                "output_dir": {
                    "type": "string",
                    "description": "可选：将生成的 Demo 文件写入该目录。",
                    "default": "",
                },
            },
            "required": ["description"],
        }

    async def execute(
        self,
        *,
        description: str,
        tech_stack: str = "",
        output_dir: str = "",
        **kwargs: Any,
    ) -> str:
        scaffold = build_vibe_scaffold(description, tech_stack)
        try:
            written = write_vibe_scaffold(scaffold, output_dir) if output_dir else []
        except Exception as e:
            return f"Vibe scaffold 写入失败：{type(e).__name__}: {e}"
        deterministic = format_vibe_scaffold(scaffold, written)

        router = self._router_getter()
        if router is None or self._run_analysis is None:
            return deterministic + "\n\n模型路由未初始化，已返回确定性 scaffold。"

        scan = scan_vibe_request(description, tech_stack, scaffold)
        user_msg = f"## Build This\n{description}\n\n## Deterministic Scaffold\n{scan}\n"
        if tech_stack:
            user_msg += f"\n## Tech Stack\n{tech_stack}\n"

        enhanced = await self._run_analysis(router, VIBE_SYSTEM, user_msg)
        return deterministic + "\n\n## LLM 增强建议\n" + enhanced
