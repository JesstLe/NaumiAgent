"""Tests for main CLI evolve command dispatch."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from naumi_agent.main import _run_evolve
from naumi_agent.tools.base import ToolCall, ToolResult


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(
            json.dumps(
                {
                    "target_file": "tools/analysis.py",
                    "new_content": "# improved content\n",
                    "description": "改进分析工具",
                },
                ensure_ascii=False,
            )
        )


class _FakeTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "自我修改已应用。"


class _EngineToolCallFake:
    def __init__(self, tool_name: str, tool: _FakeTool) -> None:
        self._router = _FakeRouter()
        self.tool_registry = {tool_name: tool}
        self.executed: list[tuple[ToolCall, str | None]] = []
        self.reload_domains: list[str] = []

    async def _execute_tool(
        self,
        tool_call: ToolCall,
        *,
        agent_name: str | None = None,
    ) -> ToolResult:
        self.executed.append((tool_call, agent_name))
        return ToolResult(
            call_id=tool_call.id,
            status="success",
            content="自我修改已应用。",
        )

    async def reload_tools(self, domain: str) -> dict[str, int]:
        self.reload_domains.append(domain)
        return {"reloaded": 1}


@pytest.mark.asyncio
async def test_run_evolve_routes_self_modify_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)

    await _run_evolve(engine, "改进分析工具")

    assert tool.calls == []
    assert len(engine.executed) == 1
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "self_modify"
    assert json.loads(tool_call.arguments) == {
        "target_file": "tools/analysis.py",
        "new_content": "# improved content\n",
        "description": "改进分析工具",
        "apply_to_workspace": True,
    }
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_uses_self_evolve_safe_apply_decision() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()

    with (
        patch("naumi_agent.tools.self_evolve.format_evolution_report", return_value="report"),
        patch(
            "naumi_agent.tools.self_evolve.run_evolution_cycle",
            return_value={
                "action": "rollback",
                "eval_result": {},
                "apply_result": {
                    "action": "rollback_blocked",
                    "message": (
                        "当前文件内容已不同于本轮评估的新内容，"
                        "为避免覆盖后续改动，拒绝自动回滚。"
                    ),
                },
                "message": "修改质量下降，但回滚未执行：拒绝自动回滚。",
            },
        ) as cycle,
        patch("naumi_agent.tools.self_modify._rollback_file") as rollback,
    ):
        await _run_evolve(engine, "改进分析工具")

    assert cycle.call_args.kwargs["apply_decision"] is True
    rollback.assert_not_called()
