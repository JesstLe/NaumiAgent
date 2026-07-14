"""Read-only Agent tools for Harness status and doctor."""

from __future__ import annotations

from typing import Any

from naumi_agent.harness.service import (
    HarnessService,
    render_harness_doctor,
    render_harness_status,
)
from naumi_agent.tools.base import Tool, ToolMetadata


def create_harness_tools(service: HarnessService) -> list[Tool]:
    return [HarnessStatusTool(service), HarnessDoctorTool(service)]


class _HarnessReadOnlyTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint="harness profile repository contract doctor trust status",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}


class HarnessStatusTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_status"

    @property
    def description(self) -> str:
        return "查看当前工作区 Harness Profile 的解析与信任状态"

    async def execute(self, **kwargs: Any) -> str:
        return render_harness_status(await self._service.status())


class HarnessDoctorTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_doctor"

    @property
    def description(self) -> str:
        return "只读诊断 Harness Profile、知识入口和检查定义，不执行命令"

    async def execute(self, **kwargs: Any) -> str:
        return render_harness_doctor(await self._service.doctor())
