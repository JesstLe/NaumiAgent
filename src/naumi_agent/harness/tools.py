"""Agent tools for Harness status, knowledge, and trusted checks."""

from __future__ import annotations

from typing import Any

from naumi_agent.harness.eval import render_harness_eval
from naumi_agent.harness.eval_surface import render_eval_baseline_status
from naumi_agent.harness.explain import render_harness_explanation
from naumi_agent.harness.service import (
    HarnessService,
    render_harness_check,
    render_harness_doctor,
    render_harness_knowledge,
    render_harness_replay,
    render_harness_status,
)
from naumi_agent.tools.base import Tool, ToolMetadata


def create_harness_tools(service: HarnessService) -> list[Tool]:
    return [
        HarnessStatusTool(service),
        HarnessDoctorTool(service),
        HarnessExplainTool(service),
        HarnessReplayTool(service),
        HarnessEvalTool(service),
        HarnessEvalBaselineTool(service),
        HarnessReadKnowledgeTool(service),
        HarnessRunCheckTool(service),
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


class HarnessExplainTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_explain"

    @property
    def description(self) -> str:
        return "解释当前工作区最近一次或指定 Harness 运行的完成与失败原因"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness explain run failure receipt check evidence why status"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "可选 Harness run id；省略或 latest 表示当前工作区最新运行",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = kwargs.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return "Harness 解释参数无效：run_id 必须是字符串。"
        try:
            result = await self._service.explain_run(run_id)
        except ValueError as exc:
            return f"Harness 解释参数无效：{exc}"
        return render_harness_explanation(result)


class HarnessReplayTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_replay"

    @property
    def description(self) -> str:
        return "安全回放当前工作区最近一次或指定 Harness 运行，不执行工具、模型或检查"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness replay deterministic receipt evidence artifact verify history"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "可选 Harness run id；省略或 latest 表示当前工作区最新运行",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        run_id = kwargs.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            return "Harness Replay 参数无效：run_id 必须是字符串。"
        try:
            result = await self._service.replay_run(run_id)
        except ValueError as exc:
            return f"Harness Replay 参数无效：{exc}"
        return render_harness_replay(result)


class HarnessEvalTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_eval"

    @property
    def description(self) -> str:
        return "运行当前 Profile 声明的离线 Harness Eval，不调用模型、命令或网络"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness eval offline protocol regression fixture suite deterministic"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1_024,
                    "description": "可选 Profile 已声明的 Suite id 或相对路径；省略表示全部",
                }
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        if suite is not None and not isinstance(suite, str):
            return "Harness Eval 参数无效：suite 必须是字符串。"
        if isinstance(suite, str) and (not suite.strip() or len(suite.strip()) > 1_024):
            return "Harness Eval 参数无效：suite 必须是 1..1024 个字符。"
        try:
            result = await self._service.eval_suites(suite)
        except ValueError as exc:
            return f"Harness Eval 参数无效：{exc}"
        return render_harness_eval(result)


class HarnessEvalBaselineTool(_HarnessReadOnlyTool):
    @property
    def name(self) -> str:
        return "harness_eval_baseline"

    @property
    def description(self) -> str:
        return "查看指定 Eval Suite 的 active Baseline 与最近权威比较回执"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=True,
            concurrency_safe=True,
            user_facing_name=self.description,
            search_hint=(
                "harness eval baseline comparison receipt regression status history"
            ),
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "suite": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Eval Suite id",
                }
            },
            "required": ["suite"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        suite = kwargs.get("suite")
        if not isinstance(suite, str):
            return "Harness Baseline 参数无效：suite 必须是字符串。"
        try:
            result = await self._service.eval_baseline_status(suite)
        except ValueError as exc:
            return f"Harness Baseline 参数无效：{exc}"
        return render_eval_baseline_status(result)


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


class HarnessRunCheckTool(Tool):
    def __init__(self, service: HarnessService) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return "harness_run_check"

    @property
    def description(self) -> str:
        return "运行当前受信任 Harness Profile 中精确声明的一项验证检查"

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            read_only=False,
            destructive=False,
            concurrency_safe=True,
            requires_confirmation=False,
            command_argument_names=(),
            user_facing_name=self.description,
            search_hint="harness validation check test lint verify completion evidence",
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "check_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "Profile 中声明的 check id",
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": "当前 Harness run 的稳定标识",
                },
            },
            "required": ["check_id", "run_id"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        check_id = kwargs.get("check_id")
        run_id = kwargs.get("run_id")
        if not isinstance(check_id, str) or not isinstance(run_id, str):
            return "Harness 检查参数无效：check_id 和 run_id 必须是字符串。"
        try:
            result = await self._service.run_check(
                check_id=check_id,
                run_id=run_id,
            )
        except ValueError as exc:
            return f"Harness 检查参数无效：{exc}"
        return render_harness_check(result)
