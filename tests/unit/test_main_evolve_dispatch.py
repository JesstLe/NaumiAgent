"""Tests for main CLI evolve command dispatch."""

from __future__ import annotations

import json
from pathlib import Path
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
        self.tool_outputs: dict[str, str] = {}

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
            content=self.tool_outputs.get(tool_call.name, "自我修改已应用。"),
        )

    async def reload_tools(self, domain: str) -> dict[str, int]:
        self.reload_domains.append(domain)
        return {"reloaded": 1}


@pytest.mark.asyncio
async def test_run_evolve_requires_self_evolve_before_modifying() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)

    await _run_evolve(engine, "改进分析工具")

    assert engine._router.calls == []
    assert engine.executed == []
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_routes_self_modify_through_engine_tool_executor() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_evolve"] = json.dumps(
        {
            "report": "report",
            "cycle_result": {
                "action": "commit",
                "apply_result": {"action": "adopted", "message": "已记录采纳决策。"},
                "message": "修改质量提升，建议提交。",
            },
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert tool.calls == []
    assert len(engine.executed) == 2
    tool_call, agent_name = engine.executed[0]
    assert agent_name == "cli"
    assert tool_call.name == "self_modify"
    assert json.loads(tool_call.arguments) == {
        "target_file": "tools/analysis.py",
        "new_content": "# improved content\n",
        "description": "改进分析工具",
        "apply_to_workspace": True,
    }
    evolve_call, evolve_agent_name = engine.executed[1]
    assert evolve_agent_name == "cli"
    assert evolve_call.name == "self_evolve"
    assert json.loads(evolve_call.arguments) == {
        "target_file": "tools/analysis.py",
        "original_content": (Path.cwd() / "src/naumi_agent/tools/analysis.py").read_text(
            encoding="utf-8"
        ),
        "new_content": "# improved content\n",
        "description": "改进分析工具",
        "apply_decision": True,
        "return_json": True,
    }
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_uses_self_evolve_safe_apply_decision() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_evolve"] = json.dumps(
        {
            "report": "report",
            "cycle_result": {
                "action": "rollback",
                "apply_result": {
                    "action": "rollback_blocked",
                    "message": (
                        "当前文件内容已不同于本轮评估的新内容，"
                        "为避免覆盖后续改动，拒绝自动回滚。"
                    ),
                },
                "message": "修改质量下降，但回滚未执行：拒绝自动回滚。",
            },
        },
        ensure_ascii=False,
    )

    with patch("naumi_agent.tools.self_modify._rollback_file") as rollback:
        await _run_evolve(engine, "改进分析工具")

    evolve_call, _ = engine.executed[1]
    assert json.loads(evolve_call.arguments)["apply_decision"] is True
    rollback.assert_not_called()
