"""Read-only Agent tools for Harness status, doctor, and repository knowledge."""

from __future__ import annotations

from typing import Any

from naumi_agent.harness.service import (
    HarnessService,
    render_harness_doctor,
    render_harness_knowledge,
    render_harness_status,
)
from naumi_agent.tools.base import Tool, ToolMetadata


def create_harness_tools(service: HarnessService) -> list[Tool]:
    return [
        HarnessStatusTool(service),
        HarnessDoctorTool(service),
        HarnessReadKnowledgeTool(service),
    ]


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


class HarnessReadKnowledgeTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_read_knowledge"

    @property
    def description(self) -> str:
        return "从当前受信任仓库知识索引按查询或相对路径读取有界证据"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            path_argument_names=("path",),
            user_facing_name=self.description,
            search_hint=(
                "harness repository knowledge read source docs instructions "
                "symbol path evidence"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "知识 ID、文件名、符号或文本查询",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "工作区内的精确相对路径",
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4_000,
                    "default": 2_000,
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        path = kwargs.get("path")
        max_tokens = kwargs.get("max_tokens", 2_000)
        if query is not None and not isinstance(query, str):
            return "Harness 知识读取参数无效：query 必须是字符串。"
        if path is not None and not isinstance(path, str):
            return "Harness 知识读取参数无效：path 必须是字符串。"
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            return "Harness 知识读取参数无效：max_tokens 必须是整数。"
        try:
            result = await self._service.read_knowledge(
                query=query,
                path=path,
                max_tokens=max_tokens,
            )
        except ValueError as exc:
            return f"Harness 知识读取参数无效：{exc}"
        return render_harness_knowledge(result)
