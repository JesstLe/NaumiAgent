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
        self.response_content: str | None = None

    async def call(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self.response_content is not None:
            return _FakeResponse(self.response_content)
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
        return json.dumps(
            {
                "report": "自我修改已应用。",
                "result": {"status": "applied", "file": "tools/analysis.py"},
            },
            ensure_ascii=False,
        )


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
            content=self.tool_outputs.get(
                tool_call.name,
                json.dumps(
                    {
                        "report": "自我修改已应用。",
                        "result": {"status": "applied", "file": "tools/analysis.py"},
                    },
                    ensure_ascii=False,
                ),
            ),
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
async def test_run_evolve_stops_when_llm_proposal_is_not_object() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(["tools/analysis.py"])

    await _run_evolve(engine, "改进分析工具")

    assert engine.executed == []
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_extracts_json_object_from_llm_explanation() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = (
        "下面是修改方案：\n"
        + json.dumps(
            {
                "target_file": "tools/analysis.py",
                "new_content": "# improved content\n",
                "description": "改进分析工具",
            },
            ensure_ascii=False,
        )
        + "\n请执行。"
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_prefers_json_fence_over_earlier_code_fence() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    proposal = json.dumps(
        {
            "target_file": "tools/analysis.py",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
    engine._router.response_content = (
        "先参考一个片段：\n"
        "```python\n"
        "print('not proposal')\n"
        "```\n"
        "真正的修改方案：\n"
        f"```json\n{proposal}\n```"
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_stops_when_llm_proposal_fields_are_not_strings() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": 123,
            "new_content": ["not", "source"],
            "description": {"bad": "shape"},
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert engine.executed == []
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_rejects_protected_target_before_self_modify() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "orchestrator/engine.py",
            "new_content": "# should never be applied\n",
            "description": "尝试修改核心引擎",
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "尝试修改核心引擎")

    assert engine.executed == []
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_rejects_unmodifiable_target_before_self_modify() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "api/app.py",
            "new_content": "# should never be applied\n",
            "description": "尝试修改 API",
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "尝试修改 API")

    assert engine.executed == []
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_prompt_lists_nested_modifiable_modules() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()

    await _run_evolve(engine, "改进自我审查工具")

    user_prompt = engine._router.calls[0]["messages"][1]["content"]
    assert "- tools/analysis_support/self_review.py" in user_prompt


@pytest.mark.asyncio
async def test_run_evolve_prioritizes_relevant_nested_context() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()

    await _run_evolve(engine, "改进 self_review 自我审查工具")

    user_prompt = engine._router.calls[0]["messages"][1]["content"]
    assert "### tools/analysis_support/self_review.py" in user_prompt


@pytest.mark.asyncio
async def test_run_evolve_matches_spaced_keywords_to_snake_case_context() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()

    await _run_evolve(engine, "改进 self review 自我审查工具")

    user_prompt = engine._router.calls[0]["messages"][1]["content"]
    assert "### tools/analysis_support/self_review.py" in user_prompt


@pytest.mark.asyncio
async def test_run_evolve_prompt_requires_target_from_candidate_list() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()

    await _run_evolve(engine, "改进分析工具")

    user_prompt = engine._router.calls[0]["messages"][1]["content"]
    assert "target_file 必须从下方可修改文件列表中选择" in user_prompt


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
        "return_json": True,
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
async def test_run_evolve_normalizes_source_prefixed_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "src/naumi_agent/tools/analysis.py",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_source_absolute_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    absolute_target = Path.cwd() / "src" / "naumi_agent" / "tools" / "analysis.py"
    engine._router.response_content = json.dumps(
        {
            "target_file": str(absolute_target),
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_backslash_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "src\\naumi_agent\\tools\\analysis.py",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_current_directory_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "./tools/analysis.py",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_module_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "naumi_agent.tools.analysis",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_src_module_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "src.naumi_agent.tools.analysis",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_line_number_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "tools/analysis.py:42",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_backticked_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "`tools/analysis.py`",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_normalizes_quoted_target_file() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": '"tools/analysis.py"',
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_accepts_file_path_target_alias() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "file_path": "tools/analysis.py",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_accepts_path_target_alias() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "path": "tools/analysis.py",
            "new_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["target_file"] == "tools/analysis.py"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_accepts_content_new_content_alias() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "tools/analysis.py",
            "content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["new_content"] == "# improved content\n"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_accepts_new_file_content_alias() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "tools/analysis.py",
            "new_file_content": "# improved content\n",
            "description": "改进分析工具",
        },
        ensure_ascii=False,
    )
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

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    modify_call, _ = engine.executed[0]
    assert json.loads(modify_call.arguments)["new_content"] == "# improved content\n"
    assert engine.reload_domains == ["tools"]


@pytest.mark.asyncio
async def test_run_evolve_reloads_modified_memory_domain() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine._router.response_content = json.dumps(
        {
            "target_file": "memory/session.py",
            "new_content": "# improved memory content\n",
            "description": "改进记忆模块",
        },
        ensure_ascii=False,
    )
    engine.tool_outputs["self_modify"] = json.dumps(
        {
            "report": "自我修改已应用。",
            "result": {"status": "applied", "file": "memory/session.py"},
        },
        ensure_ascii=False,
    )
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

    await _run_evolve(engine, "改进记忆模块")

    assert engine.reload_domains == ["memory"]


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


@pytest.mark.asyncio
async def test_run_evolve_stops_when_self_modify_status_is_not_applied() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_modify"] = json.dumps(
        {
            "report": "隔离验证通过，但主工作区未应用；这里故意包含已应用字样。",
            "result": {"status": "validated", "file": "tools/analysis.py"},
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert [call.name for call, _ in engine.executed] == ["self_modify"]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_reports_noop_without_validation_failure(capsys) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_modify"] = json.dumps(
        {
            "report": "## 自我修改结果\n**状态**: ⏭️ 无变更",
            "result": {"status": "noop", "file": "tools/analysis.py"},
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    output = capsys.readouterr().out
    assert "无变更" in output
    assert "修改未通过验证" not in output
    assert [call.name for call, _ in engine.executed] == ["self_modify"]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_reports_rejected_without_validation_failure(capsys) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_modify"] = json.dumps(
        {
            "report": "## 自我修改结果\n**状态**: ❌ 已拒绝\n**原因**: 目标受保护",
            "result": {
                "status": "rejected",
                "file": "tools/base.py",
                "error": "目标受保护",
            },
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    output = capsys.readouterr().out
    assert "已拒绝" in output
    assert "修改未通过验证" not in output
    assert [call.name for call, _ in engine.executed] == ["self_modify"]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_stops_on_malformed_self_modify_payload() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_modify"] = json.dumps(
        {
            "report": "格式错误",
            "result": "applied",
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert [call.name for call, _ in engine.executed] == ["self_modify"]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_stops_on_malformed_self_evolve_payload() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_evolve"] = json.dumps(
        {
            "report": "格式错误",
            "cycle_result": "commit",
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_stops_on_malformed_self_evolve_apply_result() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_evolve"] = json.dumps(
        {
            "report": "回滚结果格式错误",
            "cycle_result": {
                "action": "rollback",
                "apply_result": "reverted",
                "message": "不应因为 apply_result 错型而崩溃。",
            },
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_stops_on_unknown_self_evolve_action() -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_evolve"] = json.dumps(
        {
            "report": "未知动作",
            "cycle_result": {
                "action": "teleport",
                "message": "未知动作不应被当作采纳。",
            },
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    assert engine.reload_domains == []


@pytest.mark.asyncio
async def test_run_evolve_shows_rejected_self_evolve_report(capsys) -> None:
    tool = _FakeTool()
    engine = _EngineToolCallFake("self_modify", tool)
    engine.tool_registry["self_evolve"] = object()
    engine.tool_outputs["self_evolve"] = json.dumps(
        {
            "report": "## 自我进化报告\n**状态**: ❌ 已拒绝\n**原因**: 输入过大",
            "cycle_result": {
                "action": "rejected",
                "message": "输入过大",
            },
        },
        ensure_ascii=False,
    )

    await _run_evolve(engine, "改进分析工具")

    output = capsys.readouterr().out
    assert "已拒绝" in output
    assert "未知自我进化动作" not in output
    assert [call.name for call, _ in engine.executed] == [
        "self_modify",
        "self_evolve",
    ]
    assert engine.reload_domains == []
