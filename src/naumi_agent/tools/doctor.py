"""Doctor diagnostics tool."""

from __future__ import annotations

from typing import Any

from naumi_agent.tools.base import Tool, ToolMetadata
from naumi_agent.ui.doctor import render_doctor_report, run_doctor


class DoctorDiagnosticsTool(Tool):
    """Run local environment diagnostics through the shared doctor screen."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return "doctor_diagnostics"

    @property
    def description(self) -> str:
        return (
            "诊断 NaumiAgent 本机运行环境，检查 Python、配置、API key、git、"
            "rg、docker、MCP 和浏览器 daemon。"
        )

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name="环境诊断",
            search_hint=(
                "doctor diagnostics environment python config api key git rg "
                "docker mcp browser daemon"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        report = await run_doctor(
            self._engine._config,
            workspace_root=self._engine.workspace_root,
            mcp_manager=self._engine._mcp_manager,
        )
        return render_doctor_report(report)
